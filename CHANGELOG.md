# Changelog

Notable changes, newest first. Versions follow [semver](https://semver.org/):
the bridge's HTTP contract and the `h.*` helper surface are what's being
versioned, since those are what scripts depend on.

After upgrading, **re-run the plugin in Figma** — a running plugin keeps the
code it started with, so new helpers won't exist until you do. If
`plugin/manifest.json` changed, re-*import* it rather than just re-running.

## [Unreleased]

### Changed

- The light over the message field is lower. It takes less space above the
  field while Claude works.

## [2.5.0] — 2026-11-23

### Changed

- **An empty chat shows the welcome card.** Before, the welcome screen showed
  only the first time a file opened the plugin. After that, an empty chat had
  only one short line of text. Now any chat with no messages shows the
  illustration, the title and the feature rows. A cleared chat shows it too.
  The card sits in the middle of the chat. The first message replaces it.

## [2.4.1] — 2026-11-23

### Fixed

- **The chat language now changes the reply language too.** Before, after a
  language change the agent kept answering in the language of the old talk.
  Now the first message after the change tells the agent the new language,
  once. This works for Claude Code and Codex, and after a bridge restart.
  Slash commands do not use up this note. A new chat without agent replies
  does not need it.
- **Resizing the chat from the left or top edge keeps the opposite side in
  place.** Before, a drag on the left edge moved the right edge. In an older
  Figma without window position support, the window only resizes.
- **The message field fits its text after a window resize.** Before, after the
  window was narrow once, the empty field could stay tall.

### Changed

- The working animation is calmer. The three typing dots are gone, and the
  light under the message field moves slower and softer.

## [2.4.0] — 2026-11-22

### Added

- **Chat with an agent inside the plugin.** The chat icon turns the thin bar
  into a chat window. You talk to Claude Code or Codex there, and its scripts
  go to the same Figma file. There is one session per Figma file for each
  agent. The session is kept, so a restart goes on with the same conversation.
- **Agent choice: Claude Code or Codex.** The bridge finds the agents that
  are installed on its computer. It also finds Claude Code inside the Claude
  desktop app on Windows. When both are found, the default is Claude Code.
  When Figmosha was installed from Codex, the default is Codex.
- Agent detection on macOS finds Homebrew, home npm folders and the Claude
  desktop app copy. It works when the bridge starts with a short PATH.
- **Model and effort choice.** The models and the efforts come from the
  agent's own list. The default is Opus with medium effort. A change applies
  from the next message, and the chat shows a note when the agent or the
  model changes.
- Each agent runs with bypass permissions and uses your own login. Codex runs
  with bypass too, like Claude.
- With Codex the slash popup offers only `/clear`. The texts in the plugin say
  Codex when Codex is active.
- `/model`, `/effort` and `/fast` are not sent to the agent. A note says to
  pick the model and the effort in the settings.
- **No agent card.** When no agent is found, a card shows in place of the
  composer. It names both agents, gives one install command for each and has
  a Check again button.
- New bridge routes: `GET /chat/settings` and `POST /chat/settings` (agent,
  model, effort) and `POST /chat/recheck` (look for the agents again). New
  socket messages from the plugin: `chat-settings`, `chat-recheck` and
  `chat-detect`. Every tab gets a `settings` event on a change. See
  `docs/CHAT.md`.
- Claude answers stream in live. A short line shows what Claude does right
  now. The Stop button ends a turn.
- Replies show bold text, code, headings and GFM tables.
- You can add images (photos) to a message. You can quote an earlier
  message in a reply.
- Claude can offer action buttons (A, B, C, D) under a reply. One click sends
  that choice.
- Claude can ask questions in a reply. You answer each question in place.
- A slash commands popup opens when you type `/`. `/clear` starts a fresh
  session and keeps the history.
- Claude Code errors show as one clear line, for example a used up limit or
  a missing login.
- **Settings page.** The settings icon next to the chat icon opens it.
- Language: Українська (Ukrainian) or English. Claude answers in the language
  you pick, and so do the chat notes and errors.
- Theme: Like Figma, Light or Dark.
- UI scale: 90, 100, 110 or 125 percent. The window resizes with it.
- Agent, model and effort, see above.
- The settings page is a plain list. Each setting is one row: the label on
  the left, a dropdown on the right, and a divider line between rows. Every
  setting is a dropdown.
- **Welcome screen.** Each new Figma file shows a welcome screen the first
  time the plugin opens there. It has the real chat composer. It does not
  show the scale and theme line under the field.
- **First launch onboarding.** Your very first run shows four steps: what
  Figmosha does, language, theme and scale. Each choice applies at once.
- **Window resize by edges and corners.** Drag any edge or corner of the chat
  window. There are no grips or icons.
- The chat window remembers its size and its position on screen.
- **Update card in the chat.** In chat mode a new release shows a card at the
  end of the chat. Оновити opens the release notes. Пропустити hides the card
  for that version only. A stale plugin gets a re-run card instead.
  `FIGMOSHA_FAKE_LATEST=9.9.9` fakes a release for a live test.

### Changed

- **Product name.** The product name is now just Figmosha, without the word
  bridge: the plugin menu item, the window title, the hints and the docs.
  Technical ids stay the same (the manifest id, the health `service`, logs).
  If the menu still shows the old name, re-import `plugin/manifest.json`.
- **Dark theme in Figma UI3 colors.** Dark mode now uses the same neutral
  greys and blue as Figma UI3.
- **Thin overlay scrollbar.** The chat, settings and welcome screen use a thin
  scrollbar that takes no space. It shows on scroll or hover and fades out.
- The plugin bar has no colored strip. Only the status icon shows the state.
- Corner radii follow the 12px plugin window radius.
- The service banner at `GET /` reports the real bridge version. Before, it
  always said 2.0.

### Fixed

- A stale socket that closed late no longer breaks the new connection. Before,
  its close could set the bar to reconnecting while the new socket was up.
- An answer to a question is checked properly. A true or false value is no
  longer taken as a message or question number.
- A withdrawn release now clears the offer in open plugins (`update` with
  `latest: null`). Before, the bar kept offering it until a reconnect.
- The Update link check needs `figmosha2/`, so a look-alike repo such as
  `figmosha2evil` is not opened.
- After `/clear` the note names the agent that is active. Before, it always
  said Claude, also with Codex.

[2.4.1]: https://github.com/denysosadchyi/figmosha2/compare/v2.4.0...v2.4.1
[2.4.0]: https://github.com/denysosadchyi/figmosha2/compare/v2.3.0...v2.4.0

## [2.3.0] — 2026-10-06

### Changed

- **The plugin bar names its file.** Once connected it shows the Figma file's
  name instead of "connected", cut with an ellipsis if long.
- **The plugin bar counts open files.** With two or more plugin windows
  connected, the check icon sits in a white pill with this window's position,
  e.g. `1/2 ✓`. A single file shows just the icon. The bridge pushes a `peers`
  message (`{index, total}`) to every plugin when one connects or leaves; older
  plugin builds ignore it.
- **The plugin bar shows what the file is doing.** The check spins while an
  exec runs on that file (held at least 400ms so quick ones still show), and a
  failed exec turns the bar red with a `!` for 2s.
- **New plugin bar layout.** A Figma file icon and the name on the left, the
  status icon or `1/2 ✓` pill on the right.

### Fixed

- **A caller hanging up mid-run let the next caller into the file beside its
  still-running script.** The hang-up path skipped the interlock when the
  reply future was done — but cancelling `wait_for` cancels that future, so
  it always was. Rare on Python 3.12, constant on 3.14: found by the final
  Linux run of the scenario fuzzer (34–39 of 300 scenarios failed).
- **The timeout interlock was per connection, not per document**, so a caller
  routed through another tab of the same file got past it. It now covers the
  document, and `/clear` through any tab clears it.
- **A hang-up while the script was being sent** could release the lock with
  the script already on its way; the send is now always completed and the
  run interlocked.
- **`h.ck()` could not stop a tight loop.** It waited for an `abort` message
  from the bridge, but a loop that only awaits Figma APIs never lets the plugin
  read its messages. The exec's timeout now travels with the script and
  `h.ck()` checks the plugin's own clock: verified live, such a loop stops at
  its 1 s timeout instead of running on and holding the file.
- **A finished script could leave its file interlocked until `/clear`.** When
  its reply landed right at the timeout — which `h.ck()` now makes the normal
  case — the bridge dropped the reply and then marked the run abandoned.
  Both orderings of that race are handled and tested.
- **A timed-out script's `print()` lifted the interlock** while it was still
  running, letting the next caller write alongside it. Only its final result
  or error lifts it now.
- **A file could stay interlocked forever after a caller hung up.** If a
  caller disconnected in the same moment its script's reply arrived, the
  bridge marked the finished script as still running and blocked the file
  until someone POSTed `/clear`. Found by the scenario fuzzer (seed 24).
- **`start-bridge.sh` failed on a stock Mac.** It required tmux (not shipped
  with macOS) and `./venv/bin/python`, and gave up after 1 s. It now falls back
  to a plain background process without tmux, to `python3` without a venv,
  waits up to 10 s, checks `aiohttp` up front, and has `--stop`. The docs say
  `python3` / `./venv/bin/python` on macOS, which has no `python` command.
- **Every CLI call took ~2 s on Windows.** The CLI talked to `localhost`, which
  Windows resolves to `::1` first; the bridge only listened on `127.0.0.1`, so
  each request waited for the IPv6 attempt to fail. The bridge now also listens
  on `::1` (any client saying `localhost` is fast) and the CLI defaults to
  `127.0.0.1`. A CLI call went from 2157 ms to ~100 ms, an HTTP call to ~2 ms.
- **Big results were shipped twice.** The plugin sent every result as both
  `text` and `value`, and serialized objects three times on the way. It now
  serializes once and sends the value only; the bridge derives the `result`
  text (compact JSON past 1 MB, where indenting alone cost ~120 ms). A 50k-item
  result: 641 → 255 ms; a 5 MB string: 495 → 250 ms. The HTTP API is unchanged.
- **`print()` sent one message per line** through two hops; 20k lines took
  ~0.5 s. Lines are batched now (every 200 lines or 100 ms): 492 → 37 ms.
- **Two new "Untitled" files were treated as one document**, so every write
  went to the newest and the other file was unreachable. The plugin identified a
  document by its page ids, and every new file starts with page `0:1`. It now
  stores a random id in the file's plugin data once and reports that.
- **One file open in two tabs had two locks**, one per connection, so two
  writers could get into the same document at once. The lock is now per
  document.
- **A file could stay locked until the bridge restarted.** When a caller hung
  up while waiting in the queue, the bridge stopped waiting for the lock, but
  the lock request itself lived on, was granted later and never released —
  found by the new chaos test.
- **Malformed `/exec` and `/clear` bodies crashed the handler with a 500**: a
  JSON array instead of an object, a non-string `target`, a timeout that isn't
  a number. They are now a `400` naming the field; timeouts must be in
  (0, 3600] seconds.
- **A caller that gave up while queued still ran later.** The CLI's socket
  timeout ignored time spent waiting for a busy file; when it hung up, the
  bridge still ran the script once the file was free — a write nobody was
  waiting for, likely retried by the caller. The CLI now allows for the queue,
  and the bridge drops callers that left.
- Descenders in the plugin bar's text were clipped at the bottom.
- **`start-bridge.ps1` did not run in Windows PowerShell 5.1**, the one Windows
  ships with. The file had em dashes and arrows but no BOM, so 5.1 read it as
  ANSI and failed with "The string is missing the terminator". It is now pure
  ASCII.
- **The bridge did not start from a folder with a space in its path**
  (`can't open file 'F:\\00'`). PS 5.1's `Start-Process` doesn't quote
  `-ArgumentList` array elements; the arguments are now one pre-quoted string.
- `start-bridge.ps1` found the running bridge by grepping `netstat` for
  `LISTENING`, which is translated on non-English Windows, so `-Stop` and the
  already-running check silently did nothing there. It now asks
  `Get-NetTCPConnection`.
- `start-bridge.ps1` treats the Microsoft Store `python.exe` stub as "no
  Python" instead of launching it.
- The bridge forces UTF-8 on its output, so a Cyrillic, CJK or emoji file name
  can't raise `UnicodeEncodeError` when the log is redirected on Windows.
- On Windows every closed Figma tab left a `ConnectionResetError [WinError
  10054]` traceback in `bridge.err.log`. It was asyncio noise after the
  disconnect was already handled, and the bridge now drops it.
- `CLAUDE.md` told Claude to start the bridge with bash and tmux and described
  one maintainer's Mac. It now covers macOS / Linux / WSL and native Windows,
  with machine specifics left to `CLAUDE.local.md`.
- **`h.ck()` never fired.** The bridge sent `abort` for a timed-out run, but
  the plugin UI dropped every message type except `exec` and `ping`, so the
  sandbox never heard about it. The UI now forwards `abort`.
- `figmosha doctor` with several files connected and no `-T` told you to
  re-run the plugin; it now says to pick a file with `-T`, and to
  `figmosha clear` when the file is interlocked by a timed-out script.
- `--raw` and `status` printed non-Latin text as `П…` escapes.
- The `1/2` pill counted connections that had not identified themselves yet,
  so it could flash `1/3` while a tab reconnected.
- `start-bridge.ps1` exited 0 when the bridge failed to start and showed the
  stdout log, while the reason is on stderr. It now prints the tail of
  `bridge.err.log`, suggests installing the requirements when `aiohttp` is
  missing, and exits 1.
- README's Windows venv line used `&&`, which Windows PowerShell 5.1 rejects;
  helper and subcommand counts were out of date.
- The bridge test fixture reset globals that no longer exist instead of the
  multi-file registries. New tests cover the timeout interlock (504 → 409 →
  lifts when the orphan replies, `abort` sent) and the `1/2` counter.
- `tests/helpers.test.js` crashed before running a single check: `code.js`
  now reads `figma.root.name` on load and the test's Figma stub had no `root`.

### Added

- **Fuzz and hostile-input tests.** `tests/test_fuzz.py` runs random
  scenarios (tabs opening and closing, agents writing, giving up, hanging up,
  failing) and checks after each that no document ran two scripts at once and
  nothing leaked; `FIGMOSHA_FUZZ_SEEDS=300` for a long run. Plus junk from a
  misbehaving plugin, duplicate and late results, forged Host / Origin
  headers, and 3000 execs that must leave no residue. The state reset moved
  to `tests/conftest.py`, so it covers every test file.
- **Stress and chaos tests.** `tests/test_stress.py` runs 50 concurrent
  writers through a read-modify-write race (no lost update allowed), 4 files x
  15 agents, a chaos mix of impatient, failing and hung-up callers, a plugin
  dying mid-queue, a reconnect storm over one document, junk input and 5 MB
  payloads. `tests/live_stress.py` runs the same race against your real files.
- **A queue per file for several agents.** `/exec` waits its turn on the
  document's lock, and now says so: `targets` / `/status` show who is running on
  each file and who is waiting, callers name themselves with `--agent` /
  `FIGMOSHA_AGENT` / `"agent"`, replies carry `queued_ms`, and
  `--queue-timeout` / `"queue_timeout"` caps the wait with a `503 file busy`
  that guarantees nothing ran.
- **`-T` takes a document id**, shown by `targets` and stored in the file so it
  survives reconnects, so two different files with the same name (two fresh
  "Untitled" files) can each be targeted. A connection id works too.
- **Releases, and an Update button when one is out.** Figmosha now has a
  `VERSION` (in `bridge.py`, shown by `/status` and `doctor`) and is released as
  `vX.Y.Z` tags. Every 6 hours the bridge reads the release tags on GitHub; when
  a newer one exists the plugin bar turns blue with "New version X.Y.Z" and an
  **Update** button that opens that release's notes, `/status` has
  `update: {latest, current, url}`, `/exec` replies carry a `notice` and
  `doctor` says to `git pull`. A plain commit to `master` is not offered as an
  update. Silent when GitHub can't be reached; `FIGMOSHA_NO_UPDATE_CHECK=1`
  turns it off.
- **Figmosha asks you to update a stale plugin.** `plugin/code.js` carries a
  `PLUGIN_VERSION` build id that the plugin reports on connect. When it differs
  from the file on disk — after a `git pull`, say — the plugin bar turns purple
  with "New version: re-run plugin", `/status` marks the file `outdated`, every
  `/exec` reply carries a `notice`, and `figmosha doctor` says which file to
  re-run. The bridge also notices when `bridge.py` changed since it started and
  asks for a restart. `tests/test_plugin_version.py` fails if `plugin/` changes
  without a version bump. Plugins from before this release report no build id
  and count as outdated.
- **Codex and other agents can set Figmosha up.** The agent instructions moved
  from `CLAUDE.md` to `AGENTS.md`, the file Codex reads; `CLAUDE.md` is now one
  line, `@AGENTS.md`, so Claude Code loads the same text and there is nothing to
  keep in sync.
- `requirements.txt` (runtime) and `requirements-dev.txt` (adds `pytest`), so
  setup is one `pip install -r` instead of package names spread across the docs.

## [2.2.0] — 2026-08-28

### Added

- **Multiple Figma files at once.** The bridge keeps one connection per open
  file running the plugin instead of one globally. The plugin reports its
  identity and `/exec` routes by a `target` (CLI `-T` / `--target`), resolved as
  exact name → `fileKey` → unambiguous substring. `GET /targets` and
  `figmosha targets` list what's connected. One file connected with no target
  behaves exactly as before, so existing scripts don't change.
- **Per-file exec lock.** A file's plugin sandbox is a single-threaded async
  handler over one document and one undo stack, so two concurrent scripts
  interleave at every `await` and invalidate each other's `findAll` snapshots.
  `/exec` now takes a per-connection lock; callers on different files never wait
  on each other. Read-only scripts can pass `parallel: true` (`--parallel`) to
  bypass it and fan out.
- **Abandoned-run interlock.** A script that outlives its `timeout` can't be
  killed, and the next caller used to mutate the file underneath it. The `504`
  now carries a `warning` and the request id, further execs on that file return
  `409` until the orphan replies, and `POST /clear` (`figmosha clear -T <file>`)
  lifts it. `force: true` pushes past.
- **Cooperative cancellation.** `h.ck()` throws once the bridge has abandoned
  the run, so a chunked sweep stops instead of mutating under the next caller.
  `h.aborted()` is the non-throwing form. Older plugin builds ignore the signal.
- **The same document open in several tabs or windows.** Each view registers its
  own connection and `-T` routes to the newest live one; `/status` flags the
  extra views with `sameDocAs`.

### Fixed

- **One file open in two tabs looped forever.** Any second `hello` for a known
  file name evicted the incumbent, so each side kicked the other out and the
  loser reconnected 2s later and kicked back. A live connection is now never
  evicted: `hello` replaces an older same-file registration only when its socket
  is already closed or it fails a 1s liveness ping.
- **Two different files sharing a name are still refused**, and now for the
  right reason. The plugin sends a `docSig` — a hash of its page node ids, since
  `figma.fileKey` is null for a local dev plugin and `figma.root.id` is `"0:0"`
  everywhere — so the bridge distinguishes two views of one document from two
  documents, instead of treating both as ambiguous.
- **Connections registered unnamed, so `-T` could not find them.** The plugin
  UI's identity handler wrote to a `#server` element that no longer exists; it
  threw before `sendHello()` ran.
- **The bridge log stayed empty.** `start-bridge.sh` piped `bridge.py` into
  `tee` without `-u`, so Python block-buffered stdout and plugin
  connect/disconnect events were invisible exactly when they were needed.
- In-flight requests routed to a dead connection fail fast instead of hanging,
  and a dead connection drops its abandoned interlock with it.

### Removed

- The single-slot `1008` rejection and its `Slot busy` UI state, with the 15s
  backoff. The bridge no longer turns away a second connection for a file, and a
  tab that becomes visible again reconnects immediately.

## [2.1.0] — 2026-08-18

### Security

- **The bridge now refuses requests that come from a web page.** It executes
  arbitrary JS inside your open Figma file, so any tab you have open was part of
  the threat model: a page could `fetch` `localhost:8787` as a `text/plain`
  "simple request", dodge the CORS preflight, and silently edit or delete your
  work. Requests carrying an `Origin` header are now rejected, and `Host` is
  pinned to the loopback names actually served, which closes DNS rebinding.
  Local clients (curl, the CLI) are unaffected — they never send `Origin`.

### Added

- `h.sel()` and `figmosha sel` — read the current selection. Closes the gap
  between "this frame here", which you point at with a mouse, and a node id.
- `page` and `sel` work anywhere a node id is taken: `figmosha tree sel --layout`,
  `figmosha rm sel`.
- `figmosha doctor` — walks bridge → plugin → round trip → which file is open,
  naming the fix at whichever link is broken.
- `h.hex()` and `h.solid()` — hex strings instead of hand-rolled `/255` maths.
- `h.frame(parent, opts)` — creates a frame and applies auto-layout in the order
  Figma requires. Getting that order wrong fails silently, which is why it was
  worth encoding in a helper rather than documenting for a third time.
- `h.resolve(idOrAlias)` — one lookup that also understands `page` and `sel`.
- `--layout` flag on `figmosha tree` — shows layoutMode, gap, padding and sizing.
- `figmosha rm` takes several ids at once.
- `FIGMOSHA_HOST` / `FIGMOSHA_PORT` environment variables.
- `start-bridge.ps1` — detached launcher for native Windows, with `-Restart` and
  `-Stop`. `start-bridge.sh` needs bash and tmux, which a plain Windows box has
  neither of.
- Tests: the bridge is driven by a fake plugin over a real WebSocket (guard,
  exec round trip, timeouts, disconnect cleanup, slot handover, hints), and the
  pure helpers run against a stubbed Figma (hex maths, auto-layout ordering).
  Neither needs Figma.

### Fixed

- **The CLI crashed on any layer name outside cp1252** — which on a default
  Windows console means most non-English names. Output is now forced to UTF-8.
- **A reconnecting plugin could be locked out for ~20s.** A half-open socket
  (laptop slept, network changed) stayed "open" until the heartbeat gave up, and
  every 2s retry was rejected meanwhile. The bridge now pings the incumbent: no
  answer within a second and the newcomer takes over. A plugin that *is* alive
  still keeps the slot, so two Figma windows no longer evict each other forever.
  A rejected plugin shows `Slot busy` and backs off 15s instead of hammering.
- `--timeout` was ignored: the client socket deadline was hardcoded to 65s, so
  long runs died client-side while the bridge was still waiting, losing its
  error payload and hint.
- `h.var_` could not resolve a library key. `getVariableByIdAsync` rejects a
  malformed id by throwing, so the unguarded call swallowed control flow before
  the import fallback ran — the documented behaviour never worked.
- `h.bF`/`h.bS` threw an opaque `SyntaxError` from `JSON.parse(undefined)` on
  nodes with mixed fills; they now say what's wrong and which node.
- `h.withFonts` silently skipped mixed-font text nodes, so editing them failed
  later and far from the cause. It now reports what it skipped.
- Requests left in flight when the plugin disconnects are failed immediately
  rather than hanging until timeout.

### Changed

- **Plugin window is a 220×28 status bar.** Was 360×260 with a log panel. The
  background carries the state — green `Connected`, amber `Connecting…` with a
  spinner, red `Error` — using [Solar](https://www.figma.com/community/file/1166831539721848736)
  icons (CC BY 4.0). Logs moved to the plugin console.
- README rewritten: what the tool can and can't do, an HTTP API reference, a
  security section, a Mermaid architecture diagram, and installation reduced to
  handing the repo URL to Claude Code.
- Machine-specific paths and hosts moved out of `CLAUDE.md` into a gitignored
  `CLAUDE.local.md`. They had no business in a public repo that invites
  strangers to point their agent at it.

## [2.0.0] — 2026-05-20

- Replaced the Playwright + Scripter approach with the WebSocket bridge: a
  custom plugin holds a socket open to a local Python server, so Plugin API
  calls are milliseconds rather than browser automation.
- `h.*` helpers, high-level CLI subcommands, and `hint` fields on recognised
  errors.

[2.2.0]: https://github.com/denysosadchyi/figmosha2/releases/tag/v2.2.0
[2.1.0]: https://github.com/denysosadchyi/figmosha2/releases/tag/v2.1.0
