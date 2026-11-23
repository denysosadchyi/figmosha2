# Chat with an agent (Claude Code or Codex) inside the plugin

Status: built. Tests: `tests/test_chat.py`, `tests/test_providers.py`, `tests/chat_ui.test.js`,
`tests/chat_dom.test.js` (all run by `pytest -q`).

## Idea

One Figma file = one Claude Code session = one chat. The designer talks to
Claude in the plugin window. Claude can do everything it does in a terminal,
and its `figmosha.py` calls go to that same file.

The bridge runs Claude headless, one long-lived process per file:

    claude -p --input-format stream-json --output-format stream-json \
           --verbose --include-partial-messages --model opus --effort medium \
           --dangerously-skip-permissions [--resume <session_id>]

It uses the user's own Claude login (subscription). Permissions are always
bypassed. The first message starts a new session; after that the session id is
saved and every restart uses `--resume`.
`--model` and `--effort` come from the agent settings (default `opus` and `medium`).
A change of the model, the effort or the language restarts the process with `--resume`. A
language change after the agent has replied also adds a one-time note as the last text
block of the next message ("Reply in English from this message on"), because the resumed
talk pulls the agent back to its old language. The flag survives a bridge restart
(`lang_note` in the history file); slash commands do not use it up.
The same chat can also run on Codex, see Providers.

Environment for the process:

| var | value |
|---|---|
| `FIGMOSHA_TARGET` | the file's docSig, so `figmosha.py` needs no `-T` |
| `FIGMOSHA_AGENT` | `chat` (shown in the file's queue) |
| `FIGMOSHA_FILE` | the file's name |

cwd is `chat.WORKDIR` (default: the repo folder, so AGENTS.md is read).

## Providers

The bridge looks for two agents on its own computer, once, at the first use. The result is
cached until `POST /chat/recheck` (the Check again button) or `chat.reset()`.

- Claude Code: `FIGMOSHA_CLAUDE` (a command line), else `claude` on PATH, else
  `~/.local/bin/claude(.exe)`, else the newest copy inside the Claude desktop app (Windows).
- Codex: `FIGMOSHA_CODEX`, else `codex` on PATH, else the npm install in `%APPDATA%\npm`
  (the native exe, then node with `codex.js`, then `cmd /c codex.cmd`). When PATH finds the npm
  shim (`codex.cmd` on Windows), the same lookup runs in the shim's folder: a batch file breaks
  the quoted arguments. Its models come from `codex debug models`
  (visibility `list`, by priority), else `~/.codex/models_cache.json`, else the `model`
  of `~/.codex/config.toml`.

One setting for all files, in `DATA_DIR/settings.json`: `{provider, model, effort}`.
The default is the first found provider, its default model (Claude: `opus`; Codex: the
config model if it is listed, else the first one) and `medium` (or the model's default effort).
A saved provider that is not found now is used for this run only (the file keeps it, so it
comes back after a recheck). A provider change takes that provider's default model. A bad value is a 400 and changes nothing.
The settings are read when a turn starts. When the provider or the model differs from the
file's last turn, the chat gets a system note: Тепер відповідає Codex. Модель GPT-5.5.

Each provider keeps its own session per file: `sessions.json` (Claude) and
`sessions-codex.json` (Codex thread ids). `/clear` drops the session of the current provider.

### Codex runner

One process per turn, cwd `WORKDIR`, the prompt on stdin:

    codex exec [resume] [-i <photo>]... --json --skip-git-repo-check \
          --dangerously-bypass-approvals-and-sandbox -m <model> \
          -c model_reasoning_effort=<effort> -c developer_instructions=<system prompt> [<thread_id>] -

`-i` goes right after `exec`, because it takes several values. The env is the bridge's env
without `CLAUDECODE`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CODEX_CI`, plus the `FIGMOSHA_*` vars.
Events (JSON lines):

- `thread.started`: the thread id is saved.
- `item.started`: an activity line. A command shows the command without the shell wrapper,
  any other item its type.
- `item.completed` `agent_message`: a Claude style message (actions and questions blocks work).
- `turn.completed`: the turn ends. `turn.failed`: an error note with the API code and message.
- top level `error`: kept; shown only when the process ends with no turn end.
- no rollout found on resume: the thread is dropped, a system note says so, the turn runs again fresh.

Stop terminates the process tree; after `STOP_GRACE` the tree is killed. No line is added.
Codex starts in its own process group (POSIX) and Windows uses `taskkill /T`, because the npm
wrapper runs the native binary as a child that a kill of the wrapper alone leaves running.
Slash commands: only `/clear` (and its aliases). Any other one gets a note: Команда /x працює лише
з Claude Code. Photos go as `-i` files.

### No agent

With no provider found, a message is stored, then an error note says: Не знайшов Claude Code чи
Codex на комп'ютері з мостом. Встанови один із них і увійди в нього в терміналі. The chat is not busy.
The plugin shows the card `#cNoAgent` in place of the composer (in the chat, or in the welcome
screen): both names, one install command for each, and Check again (`chat-detect`).

## Module `chat.py`

Imported by `bridge.py`; `build_app()` adds its routes and runs
`chat.shutdown()` on cleanup.

Settings (tests override them):

- `DATA_DIR` — `sessions.json` ({doc: session_id}), `history/<doc>.json`, `uploads/`,
  `commands.json` (Claude's slash commands, one list for all files)
- `CLAUDE_CMD` — default `["claude"]` (`FIGMOSHA_CLAUDE`)
- `CODEX_CMD` — default `["codex"]` (`FIGMOSHA_CODEX`)
- `settings.json` and `sessions-codex.json` in `DATA_DIR`, see Providers
- `WORKDIR` — default the repo folder
- `STOP_GRACE` — seconds to wait after an interrupt before killing the process
- `HISTORY_LIMIT` — messages kept per file (memory and disk)
- `SESSIONS` — {doc: Session}; a Session has `.proc` and `.busy`

Functions:

- `reset()` — forget all in-memory state (does not touch DATA_DIR)
- `async shutdown()` — end every Claude process
- `parse_reply(text) -> (clean_text, actions, questions)` — takes out fenced
  ` ```actions ` and ` ```questions ` blocks, one item per line; at most 4 actions
- `activity_line(tool_use) -> str` — Bash: its description, else `Bash: <cmd>`;
  file tools: `<Tool> <basename>`; anything else: the tool name; at most 120 chars

## HTTP

All routes use the bridge guard: Origin `null` (the plugin) and no Origin
(local tools) pass, any web page gets 403. `doc` must match `[A-Za-z0-9_-]{1,64}`,
else 400.

| route | body / query | answer |
|---|---|---|
| `GET /chat?doc=` | | `{ok, doc, messages, busy, activity, session, commands, lang, providers, settings, models}` |
| `GET /chat/settings` | | `{ok, providers, settings, models}` |
| `POST /chat/settings` | `{provider?, model?, effort?}` | the same shape; 400 with `error` on a bad value. Every tab gets a `settings` event |
| `POST /chat/recheck` | | look for the agents again; the same shape; every tab gets a `settings` event |
| `POST /chat/send` | `{doc, text, images?, quote?, answer?, lang?}` | `{ok, id}`; 400 if no text and no image |
| `POST /chat/stop` | `{doc}` | `{ok}`; harmless when idle |
| `GET /chat/uploads/<file>` | | the saved image; no path escape |

`providers` is `[{id, name, path, model, models}]`, found ones only, Claude Code first. A model is
`{id, name, efforts, default_effort}`. `models` is the same lists by provider id. `session` and
`commands` are those of the current provider.

`/status` gets `chat: {busy}` on each file that has a session.

Message: `{id, from: me|claude|system, text, t, images?, quote?, answer?, actions?, questions?, error?}`.
Ids grow per file and keep growing after a restart.

- `images`: data URLs (png, jpeg, gif, webp). Saved to uploads, shown as
  `/chat/uploads/...`, sent to Claude as image blocks next to the text block.
  Anything else is dropped.
- `quote`: id of an earlier message. Stored as `{id, from, text}`; the quoted
  text goes to Claude too.
- `answer`: `{msg, i}` = answer to question i of message msg. Stored with the
  quote `Питання {i+1}. <question>`; Claude gets the question with the answer.
  Ignored if that question does not exist.

Busy rules: a message sent while busy waits and goes to the same process in
order. Stop sends an interrupt control request; if the turn does not end in
`STOP_GRACE` seconds the process is killed and the next message resumes the
session. Stop adds no line to the chat. A crash or a missing `claude` binary
adds a `system` message with `error: true` and the stderr text.

Slash commands. `commands` is `[{name, description, hint, aliases}]`. It comes
from Claude's `init` and `commands_changed` events and is cached in
`commands.json`. Names with spaces (MCP prompts) are dropped. Before Claude
ever ran, a built-in fallback list is used. A text that starts with `/name`
and has no images and no answer is a command:

- `/clear`, `/reset`, `/new`: handled by the bridge. It ends the process, drops
  the stored session id and adds a system note. The history stays. The next
  message starts a fresh session. Queued in order when busy.
- terminal-only (`/help`, `/exit`, `/resume`, `/login` and similar): not sent,
  a system note says so.
- unknown name (only once the real list is cached): not sent, a system note says so.
- `/model`, `/effort`, `/fast`: not sent, a note says to pick the model and the effort in the settings. Not in the list.
- anything else goes to Claude as typed, without the quote. `/compact` shows
  the activity line while Claude compacts and a system note on `compact_boundary`.

These notes have no `error` flag. The UI draws them neutral (`.msg.system.info`).
`/tmp/x` and `//x` are plain text.

Claude Code errors are shown in Ukrainian by `chat.error_text(kind, raw)`: one
clear line, then the original text as the detail line. The kind is the
assistant event `error` field (`authentication_failed`, `rate_limit`,
`billing_error`, `server_error`, `invalid_request`) or the result subtype
(`error_max_turns`); if there is none, the text is matched. The CLI's own
English error bubble (`is_api_error_message`) is not stored as a Claude message.

## Error states

Every state has a test. The chat never stays busy, and every message to the
user is Ukrainian. Python tests are in `tests/test_chat_errors.py` unless a file
is named; UI tests are checks in `tests/chat_dom.test.js` (function
`errorStates` unless named) and `tests/chat_ui.test.js`.

| State | What the user sees | Test |
|---|---|---|
| no `claude` and no `codex` | error note: Не знайшов Claude Code чи Codex ... Встанови один із них. Again on every try. The plugin shows the no agent card. | `test_missing_claude_binary_says_how_to_fix_it`, `test_chat.py::test_missing_claude_binary_is_a_clear_error` |
| `claude` cannot run (not executable) | error note: Не вдалося запустити Claude Code | `test_claude_that_cannot_run_is_a_clear_error` |
| Not logged in | one error note: Claude Code не увійшов в акаунт ... виконай /login. No English Claude bubble. | `test_not_logged_in_is_one_clear_line` |
| Subscription limit used up | error note: Ліміт підписки Claude вичерпано, with the reset time | `test_used_up_subscription_limit_is_explained` |
| Billing, overload, bad request, unknown API error | error note per kind (оплата, сервер, не прийняв запит, generic) | `test_api_errors_are_explained`, `test_error_text_is_clear_ukrainian` |
| Too many turns (`error_max_turns`) | error note: Claude зробив забагато кроків | `test_too_many_turns_result_is_explained` |
| `error_during_execution` with `errors` list | error note with those errors | `test_execution_error_shows_its_errors_list` |
| Broken output lines (not JSON, odd shapes, bad UTF-8) | nothing; they are skipped, the reply comes | `test_broken_output_lines_are_skipped` |
| Claude exits in the middle of a turn | error note with stderr; next message resumes | `test_exit_in_the_middle_of_a_turn_is_reported`, `test_chat.py::test_a_crash_is_reported_and_the_next_message_resumes` |
| Crash after part of the reply | the part already written stays, plus an error note | `test_crash_after_part_of_the_reply_keeps_that_part` |
| Claude dies while idle | nothing; the next message resumes the session | `test_process_dying_while_idle_is_silent_and_the_next_message_resumes` |
| Resume of a session that no longer exists | note: Стару розмову з Claude не знайдено. Почав нову. Then the reply. | `test_lost_session_starts_a_new_one_and_answers` |
| Hanging turn | typing line and seconds; Stop ends it (kill after `STOP_GRACE`) | `test_chat.py::test_stop_kills_a_session_that_ignores_the_interrupt` |
| Stop during a turn | the turn ends, no chat line | `test_chat.py::test_stop_interrupts_the_turn_without_a_chat_line` |
| Stop while Claude is still starting | the turn ends, chat idle | `test_stop_while_claude_is_still_starting` |
| Stop twice, then send again | idle, then a normal reply | `test_stop_twice_then_send_again` |
| Stop when idle | nothing | `test_chat.py::test_stop_when_idle_is_harmless` |
| Sending while busy | message waits, answers come in order | `test_chat.py::test_messages_sent_while_busy_are_answered_in_order` |
| Window reopened mid-turn | busy state, step and time come back; Stop works | `test_window_reopened_mid_turn_gets_busy_and_can_stop`, `busyStreamStop` (opened mid-task) |
| Reply lands while the window is closed | the reply is in history on open | `test_reply_that_lands_while_the_window_is_closed_is_in_history` |
| Bridge stops mid-turn | after restart: error note Міст зупинився, поки Claude працював. Not busy. | `test_bridge_restart_mid_turn_leaves_a_note` |
| Socket drops mid-turn, then reconnects | Stop gives toast Немає зв'язку з мостом; on return the chat reloads and busy follows history | `errorStates` |
| Stop pressed, socket dropped before it arrived | after reconnect Stop can be pressed again | `errorStates` |
| Stop cannot be sent | toast Не вийшло зупинити; Stop works again | `errorStates` |
| Bridge down at plugin start | send and Stop: toast Немає зв'язку з мостом; text stays | `errorStates`, `sending` |
| Send throws on the socket | toast Не вийшло надіслати; text stays | `errorStates` |
| Send refused by the bridge | toast Не вийшло надіслати; text comes back | `sending` |
| Broken data on the socket | nothing; no script error | `errorStates` |
| Window has no file id yet | send is refused: toast Не вийшло надіслати | `test_window_without_a_file_id_is_refused_not_ignored` |
| Chat code fails on the bridge | send refused; the plugin socket stays up | `test_a_chat_failure_does_not_drop_the_plugin_socket` |
| Unknown or bad doc id over HTTP | empty idle chat / 400 | `test_chat.py::test_unknown_doc_has_empty_idle_chat`, `test_chat.py::test_bad_doc_is_rejected` |
| Photo too big or broken | error note: Фото не додано. The text is still sent. Photo only: refused. | `test_too_big_or_broken_photo_is_dropped_with_a_note`, `test_chat.py::test_bad_images_are_dropped` |
| Odd `images` field (not a list, nulls) | nothing breaks; text is sent | `test_odd_images_field_never_breaks_the_chat` |
| Photo cannot be saved (disk) | error note: Фото не додано | `test_photo_that_cannot_be_saved_is_dropped_with_a_note` |
| Photo cannot be opened in the UI | toast Не вийшло відкрити фото | `photos` |
| History file corrupt | a clean chat | `test_corrupt_history_file_starts_a_clean_chat` |
| History file with wrong shapes | good messages load, new ids stay unique | `test_history_file_with_wrong_shapes_still_loads` |
| sessions.json corrupt | a new session starts | `test_corrupt_sessions_file_starts_a_new_session` |
| History cannot be saved (disk) | one error note: Не можу зберегти історію чату на диск. The chat goes on. | `test_history_that_cannot_be_saved_does_not_block_the_chat` |
| Two plugin windows on one file | both see the same chat | `test_chat.py::test_same_file_in_two_tabs_both_see_the_chat` |
| Events of another file | ignored | `loadAndModes` |
| clientStorage off in Figma | the plugin opens as the bar; the chat icon still works | `chat_ui.test.js` modeTests (storage off) |
| Error note in the chat | red system row, no quote button | `errorStates` |

Not covered on purpose: a turn has no timeout (the user stops it); a refused
send puts the text back but not the photos; one stdout line over 64 MB is
skipped; a WS message over 16 MB closes the socket (photos are shrunk first).

## Live events

Pushed over the plugin WebSocket, only to tabs whose docSig is that file
(two tabs of one file both get them):

    {"type": "chat", "doc": "<docSig>", "event": {...}}

| kind | fields | when |
|---|---|---|
| `message` | `message` | a message is stored (mine, Claude's, system) |
| `busy` | `busy` | a turn starts / ends |
| `activity` | `text` | a tool call starts (stored entries also have `t`) |
| `delta` | `text` | streamed text of the current reply |
| `commands` | `commands` | Claude's command list changed |
| `settings` | `providers, settings, models, commands, error?` | the agent settings changed, a recheck, or an answer to `chat-settings`. Sent to every tab, each with its own doc |

Socket messages from the plugin for the settings: `{type: "chat-settings"}` (get, answer to the
asker), `{type: "chat-settings", provider?, model?, effort?}` (set; on a bad value only the asker
gets the event with `error`), `{type: "chat-detect"}` or `{type: "chat-recheck"}` (look again).
The plugin also accepts the kind `providers` with the same fields.

Activity is kept only while busy (last 30) and cleared when the turn ends.

## Plugin window

Settings page layout: a plain list of rows on the page, no cards. Each row (`.srow`) has the
label (`h3`) on the left and a native `<select>` on the right (at most 60% of the row). Visible rows
are split by a 1px `--line` divider; a hidden row adds none. Every setting is a dropdown.

Settings page, agent part: three dropdowns after the language, Агент
(Claude Code, Codex), Модель and Зусилля. A provider that is not found is a disabled option. The
models and the efforts come only from the bridge. A change sends one `chat-settings` over the
socket (never a `settings` post to code.js); the selected option follows the bridge's `settings`
event, and a re-render sends nothing and keeps the focus.
With an older bridge (no `providers` in history) the chat works as before. With provider
Codex the slash popup shows only `/clear`.

Two states, switched by the chat icon:

- bar: 220x28, the plugin strip: neutral, the state is only in the colored status icon `#icon` (class `st-ok|st-busy|st-err|st-update|st-conn`, title and aria-label in words); buttons on the right live in `#barActions`
- chat: 380x560 by default, resizable by its edges (see Window size), the board chat design

`code.js` remembers the state in `figma.clientStorage` key `figmosha-ui-mode`
and restores it on start. The UI asks with `{type: "ui-mode", mode}`; code.js
resizes, stores, and answers `{type: "ui-mode", mode}`. Unknown modes are ignored.

The settings icon next to the chat icon (chat mode only) opens the settings
page: interface scale (90, 100, 110, 125 percent), theme (System, Light,
Dark) and language (Українська, English), each a dropdown (`#sScale`, `#sTheme`, `#sLang`).
A change sends one message and keeps the focus on the dropdown. The UI sends `{type: "settings", scale, theme, lang}`; code.js stores it in
`figma.clientStorage` key `figmosha-settings` and resizes the window (bar and
chat) by the scale. On start code.js keeps the window hidden (`visible: false`),
reads both keys, sends `{type: "settings", ...}` (defaults 1, `system` and `uk` when
empty or invalid; settings stored before lang existed give `uk`), resizes, then shows the window. The UI zooms the page with
`--ui-scale` on `<html>`. System follows the `figma-dark` class Figma puts on
`<html>` (`themeColors: true`); the dark color set is `:root.dark`.
The dark set copies Figma UI3 dark: neutral greys (panel `#2c2c2c`, list
`#1e1e1e`, hover and own bubble `#383838`, borders `#444444`/`#545454`), white
text, muted `#b3b3b3`, Figma blue `#0d99ff` (filled buttons `#0a6dc2` for
white text contrast). Values are plain hex, not `--figma-color-*`, because Dark
can be picked while Figma is light. Rules outside the two token blocks hold no
color literals: shadows, the photo remove overlay and the glow go through
tokens (`--compose-shadow`, `--pop-shadow`, `--scrim`, `--on-scrim`,
`--glow-a`, `--glow-b`). Fills nested in a panel (code, pre, table head, chip
number, choice icon, header badge) use `--well`, Figma secondary `#383838`;
inside the own bubble it is `--well-me` `#444444`. In dark the toast is a
`#383838` pill with a ring (`--toast-*`). Light values are unchanged. `tests/colors_lint.test.js`
checks that no raw color is used outside the two token blocks.

Window size. In chat mode (the settings page too) the window is resized by
dragging any edge or corner. There are no grips or icons: eight empty
`.rz` zones (`data-edge` n s e w ne nw se sw, 6px edges, 4px on the right,
12px corners; the overlay scrollbar thumb sits just left of the right zone,
see Scrollbars) sit above everything with the resize
cursors. They are `display:none` in bar mode, and the bar is never resized.
The UI captures the pointer on the zone and sends `{type: "resize", w, h}`
at most once per animation frame, then `{type: "resize", w, h, done: true}`
once on pointerup. w and h are CSS px at scale 1: start size
(`innerWidth/innerHeight`) plus the `screenX/screenY` delta, divided by the scale.
code.js acts only in chat mode, ignores anything but two finite numbers, clamps
the base size to 320x400 .. 900x1400, calls `figma.ui.resize(w*scale, h*scale)`
and on done stores `{w, h}` in `figma.clientStorage` key `figmosha-chat-size`.
On start and on the chat icon chat mode uses that size (an invalid value gives
380x560). A scale change keeps the base size. Left and top edges: the message also
carries `edge`, and for w, nw, sw, n, ne code.js keeps the opposite side in
place. At the first step of a drag it reads `getPosition().canvasSpace` and the
window size, then after each resize calls `figma.ui.reposition` moved by the
size change / zoom. Done clears that anchor. Old Figma without getPosition or
reposition only resizes. A window resize also refits the composer height. A drag whose
pointerup is lost outside the iframe ends on the next move with no button held.

Window position. code.js keeps the chat position per user in
`figma.clientStorage` key `figmosha-chat-pos` as WINDOW space `{x, y}`
(`figma.ui.getPosition().windowSpace`, px from the Figma window corner), so it
is the same screen spot whatever the canvas zoom or scroll. Chat mode only: the
bar stays where Figma puts it and its position is never stored. There is no
window-moved event, so in chat mode code.js polls `getPosition` once a second
(one `setInterval`, stopped on the switch to bar and on `figma.on("close")`).
The spot read when chat mode starts (after the restore) is the baseline. A new
spot is stored once it held still for one tick, so a window that does not
move writes nothing. It is also stored at once on `{type: "resize", done}` and
right before the switch to bar (only when it differs from the stored one).
Restore: on start in chat mode (the welcome screen too) and on the bar to chat
switch, after the resize, a stored value with two finite numbers is clamped to
x in [0, max(0, bounds.width*zoom - 80)], y in [0, max(0, bounds.height*zoom - 40)]
of the current `figma.viewport`, converted to canvas space
(`bounds.x + x / zoom`, `bounds.y + y / zoom`) and passed to
`figma.ui.reposition`. Window space starts at the Figma window corner, but the
viewport starts below the toolbar and right of the left panel. So when
`getPosition` gives both `windowSpace` and `canvasSpace`, code.js measures that
offset (ox, oy) from the current spot first: the clamp becomes
[0, max(0, ox + bounds.width*zoom - 80)] (same for y), and the canvas point is
`bounds.x + (x - ox) / zoom`. Without it the offset is 0. Without this the
window would drift by the panel size on every run. A bad value is ignored. Older Figma without
`getPosition`, `reposition`, `viewport` or `figma.on`, a throwing
`getPosition` and storage errors are all ignored: nothing throws.

Language. All UI texts live in `I18N = {uk, en}` in `ui.html` and go through
`t(key, vars)`. A switch re-renders at once, sets `<html lang>`, and sends
`{type: "chat-load", lang}` again. The bar status line (Connecting, New version)
stays English in both. code.js keeps lang too: the re-run hint follows it.
The bridge gets lang on `chat-load` and `chat-send` (socket) and `/chat/send`
(HTTP). A missing or bad lang keeps the doc's stored one (`uk` by default).
Each doc keeps lang in `history/<doc>.json`, and `history` events carry it.
lang sets the system prompt line (Reply in Ukrainian / Reply in English) and the
language of the bridge notes and errors. On a change the bridge closes Claude
gently at the next turn and starts it again with `--resume`: the session stays.
Uk texts are the same as before.


Scrollbars. The message list, the settings page and the welcome screen hide the
native scrollbar (`scrollbar-width:none`, `::-webkit-scrollbar` display none), so
the content uses the full width (the list has 12px padding on both sides). JS adds
an empty `aria-hidden` rail `.sbar[data-for=<area id>]` next to each area (not
inside it, so innerHTML renders keep it), absolute, `right:4px` (left of the e zone,
z-index below the `.rz` zones), `pointer-events:none`, no track. Its thumb
`.sbar-thumb` is 3px wide, 6px on hover or drag, a pill in a `--faint` mix.
Height is max(24, ch*ch/sh), place follows scrollTop; it updates on scroll,
ResizeObserver, window resize and a MutationObserver on the area (streaming too).
All of these only queue one update per animation frame, and unchanged values are not
written again, so a streaming reply does not force a reflow per chunk.
The rail is hidden without overflow, gets `on` on scroll or hover and fades out 1 s
after the last scroll (not during a drag; no transition with reduced motion).
Dragging the thumb (button 0, pointer capture, one pointerId) scrolls by
dy*(sh-ch)/(ch-thumb), with dy divided by the html zoom (UI scale) taken from
getBoundingClientRect/offsetHeight, so the thumb tracks the pointer at any scale; a
click does not jump. The `.cmds` popup keeps a thin native bar.

### Notes

A note is a task pinned to a node. It is a native Figma annotation
(`node.annotations`) in the category `Figmosha` (violet), so Figma draws it on
the canvas next to the node: it is not a layer and it is not in exports. A done
note is in the category `Figmosha · done` (green); its label is the task, then
`\n\n✓ ` and the agent's reply. code.js creates both categories on the first
use. The plugin API writes annotations on any plan (the Figma tool itself needs
a paid plan and a Full seat), so the plugin window is the way to add a note.
Annotations exist on frames, components, instances, shapes and text; a group, a
section or a page gives the toast На цей шар не можна.

Helpers (`h.notes`, `h.note`, `h.noteDone`, `h.noteClear`) and `figmosha notes`
are in AGENTS.md. `h.notes()` scans the current page once (about 2 s on 60k
nodes), so the list loads when asked for, never on its own.

Window. The pin button `#cPin` in the message field sends
`{type: "note-add", text}` to code.js with the typed text; code.js pins it to
the first selected node and answers `{type: "notes", items, error, added}`. The
field is cleared only if it still holds that text. The icon `#notebtn` in the
header (chat mode only) opens the page `#notes` (`html.notes-open`), which
replaces the chat like the settings page; only one of the two pages is open.
On open, and when a turn ends while the page is open, the UI sends
`{type: "notes-load"}`. From then until `{type: "notes-close"}` code.js follows
the canvas (`nodechange` of the current page, and the page switch) and sends
the list again when an annotation, a name, or the set of nodes changes, so a
note written in Figma itself, by the agent or from the CLI shows up at once.
Scans are spaced by three times the last scan, at least 300 ms. Rows: the node name, the task, the reply of a done
note. A click on a row sends `{type: "note-focus", id}` (select and zoom). Do
(`data-n="run"`) and Do all (`#nRunAll`, open notes only) send one ordinary
chat message built by `ChatCore.notePrompt(items, lang)`: the tasks with their
node ids and note indexes, and the `h.noteDone` call to make after each one.
So there is no second queue: the chat already does messages in order. Remove
(`data-n="rm"`) sends `{type: "note-remove", id, i, text}`; code.js removes the
note only where the text still matches, because the list can be stale.
Errors come as `{type: "note-error", code}` with `nosel`, `unsupported`, `gone`
or `fail`, and the UI shows a toast. These messages stay in the window: they
never go to the bridge.

The feature shows only where it can work. `identity` carries `notes`: true in a
design file whose Figma has `figma.annotations`, false in FigJam, Dev Mode and
an old Figma. Without it the header icon and the pin button are hidden
(`html.has-notes` shows them). A plugin cannot read the plan or the seat, and
it does not need to: it writes annotations on any plan. But on a free plan
Figma draws no annotations on the canvas (the notes then live only in the tasks
page), so the user can switch the feature off: the settings page has the row
`#sNotesRow` with the dropdown `#sNotes` (On, Off), shown only where notes can
work. The choice goes to code.js as `{type: "notes-setting", on}`, is stored in
`clientStorage` under `figmosha-notes` and comes back in the same message on
start. It is a message of its own, so `settings` keeps its shape.

### Welcome screen

The first time a Figma file opens the plugin, the chat window shows a welcome
screen `#welcome` (role `dialog`): an inline SVG illustration (`svg.willo`,
colors only from the CSS tokens), a title, one intro line, five feature rows
`.wrow` with Solar icons, the chat composer at the bottom and a link to the
settings. The composer is the real `#cCompose` (field, paperclip, send): it
moves into `#welcome .wcomp` while the screen is open and back into `#chat`
when it closes, so the draft and the photos stay. The field has the focus. All its strings are in `WELCOME_TEXT = {uk, en}` in `ui.html`. A language switch re-renders it.

- The flag is per file: `figma.root` pluginData key `figmosha-welcome`. Any
  non-empty value means seen. It is not in `clientStorage`, so another file
  that never saw the screen shows it again.
- On start code.js sends `{type: "welcome", show}` once, after `settings`
  (which keeps its shape `{type, scale, theme, lang}`). `show` is false when the flag
  is set or can not be read. When `show` is true, code.js resizes to the chat
  size (times the scale) and posts `{type: "ui-mode", mode: "chat"}` before it
  shows the window. This mode is not stored.
- A sent message (Enter or the send button, empty text does nothing), Esc,
  or the settings link close the screen. The message goes to the chat as usual. The UI posts
  `{type: "welcome-done"}` once and goes on to the normal chat (or to the
  settings). code.js writes `"1"` to the flag. Plugin data errors are caught.
- Tests: `tests/welcome_code.test.js` (code.js), `tests/welcome_dom.test.js` (UI), `tests/welcome_composer.test.js` (the composer).

### Empty chat

A chat with no messages and no stream shows the same card as the welcome
screen in `#cMsgs .hint.wempty`: the illustration, the title, the intro line
and the feature rows (`welcomeBody()` in `ui.html`). It has no composer and no
`welcomeH` id; the composer stays in its normal place. The art pattern id is
`wDotsE`, so it never doubles the id of the open welcome screen. The card shows
for a new file after the welcome screen closes and for any file whose history
is empty (a cleared chat too). The first message or stream replaces it.

### Onboarding

The very first run of a USER (any file) shows the onboarding `#onboard`
(role `dialog`) instead of the welcome screen. It has four steps `.ostep`:

1. What Figmosha with Claude does: an inline SVG illustration (chat panel and
   canvas, tokens only, its own pattern id `oDots`) and three rows with Solar icons.
2. Language: `[data-lang]` radios Українська / English.
3. Theme: `[data-theme]` radios system / light / dark.
4. Scale: `[data-scale]` radios 90% / 100% / 110% / 125%.

Each choice applies at once through the normal `settings` message
`{type, scale, theme, lang}`, so the screen re-renders (language) or the window
resizes (scale) on the same step. Progress dots `.odot` mark the step
(`aria-current="step"`). Each radio group has one Tab stop, the checked card;
arrow keys move and pick. `#onBack` (hidden on step 1), `#onNext` (Далі, Почати on
step 4) and `#onSkip` (Пропустити) sit in sticky bars, so the screen fits
380x560 at scale 1 and 1.25. All strings are in `ONBOARD_TEXT = {uk, en}`.

- The flag is per user: `figma.clientStorage` key `figmosha-onboarded`.
- On start code.js sends `{type: "onboarding", show}` once, after `settings` and
  before `welcome`. When `show` is true, code.js opens the chat size and sends
  `welcome` with `show: false`. The UI also ignores a `welcome` on that run.
- Почати on step 4, Enter on it, Пропустити, Esc or the settings button finish.
  The UI posts `{type: "onboarding-done"}` once (never `welcome-done`) and
  focuses the chat input. code.js stores the user flag and writes `"1"` to the
  file flag `figmosha-welcome`, so this file shows no welcome after it.
- Tests: `tests/onboard_code.test.js` (code.js), `tests/onboard_dom.test.js` (UI).
  The other harnesses seed `figmosha-onboarded: 1` in their fake
  `clientStorage`, as a user who already finished the onboarding.

`ui.html` keeps the pure chat logic in `<script id="chat-core">` as `ChatCore`:

- `renderText(s)` — escape HTML, then `**bold**`, `` `code` ``, fenced code as
  `<pre>`, newlines as `<br>`
- `actionLetter(i)` — A, B, C, D
- `initialState()` — `{messages: [], busy: false, activity: [], stream: "", commands: []}`
- `matchCommands(list, text)` — commands for the popup while the text is `/` and a
  name with no space: prefix matches first (aliases count), then substring
  matches; names starting with `_` hidden
- `commandHint(c)` — Ukrainian hint for known commands, else the first sentence
  of Claude's description
- `FALLBACK_COMMANDS` — used when the bridge sent no list
- `reduce(state, event)` — returns a new state for `history`, `message`
  (dedupe by id; Claude's message clears the stream), `busy` (clears activity
  and stream), `activity` (last 30), `delta` (appends to stream), `commands`
  (history keeps the old list when it has none)

Slash popup `#cCmds` above the compose box: arrows move, Enter or Tab or a click
picks (the input becomes `/name `), Esc closes it and keeps the text. Enter on a
fully typed name sends it.
