# Changelog

Notable changes, newest first. Versions follow [semver](https://semver.org/):
the bridge's HTTP contract and the `h.*` helper surface are what's being
versioned, since those are what scripts depend on.

After upgrading, **re-run the plugin in Figma** — a running plugin keeps the
code it started with, so new helpers won't exist until you do. If
`plugin/manifest.json` changed, re-*import* it rather than just re-running.

## [Unreleased]

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

- **"New version" with an Update button.** The bridge asks GitHub every 6
  hours how many commits this checkout is behind `master`. When there are new
  ones, the plugin bar turns blue with "New version" and a white **Update**
  button that opens the CHANGELOG on GitHub; `/status` has an `update` field,
  `/exec` replies carry a `notice` and `figmosha doctor` says to `git pull`.
  It stays silent when GitHub can't be reached or `HEAD` is a local commit, and
  `FIGMOSHA_NO_UPDATE_CHECK=1` turns it off.
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
