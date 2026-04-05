# Figmosha

AI-driven Figma design automation. Claude Code draws UI designs in Figma by executing Plugin API scripts through browser automation (Playwright + Scripter plugin).

## How it works

```
Claude Code  →  run.py (Playwright/Firefox)  →  Figma Scripter  →  Figma canvas
```

1. `run.py` launches Firefox, logs into Figma, and opens the Scripter plugin
2. Claude Code sends Figma Plugin API scripts via a named pipe (`/tmp/figmosha.fifo`)
3. Scripter executes the code inside Figma — creating frames, components, text, auto layouts
4. Canvas state is read back via `output.txt` dumps (no screenshots needed)

## Setup

### Prerequisites

- Python 3.10+
- Playwright (`pip install playwright && playwright install firefox`)
- Xvfb for headless Linux (`sudo apt install xvfb`)
- A Figma account with the [Scripter](https://www.figma.com/community/plugin/757836922707087381) plugin installed

### Linux (AppArmor fix)

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

### Virtual display

```bash
Xvfb :99 -screen 0 1920x1080x24 &
```

### Start the server

```bash
DISPLAY=:99 python -u run.py --serve FIGMA_FILE_URL EMAIL PASSWORD
```

### Execute code

```bash
# Inline
python run.py "figma.createRectangle()"

# From file
python run.py --file script.js
```

## Project structure

```
run.py          — Playwright server: browser automation + fifo listener
scripter.md     — Comprehensive rules for generating Figma Scripter code
CLAUDE.md       — Instructions for Claude Code sessions
plugin/         — Custom Figma plugin (alternative to Scripter)
  code.js       — Plugin backend (eval + print)
  ui.html       — Plugin UI (code editor + output)
  manifest.json — Plugin manifest
```

## Key concepts

- **Components page**: All reusable Figma components live on a dedicated "Components" page, not on the working canvas
- **No screenshots by default**: Canvas state is inspected via tree dumps to `output.txt`, saving ~1000 tokens per operation
- **Clipboard paste**: Code is injected via clipboard (`navigator.clipboard.writeText` + Ctrl+V) for speed
- **Cross-page components**: `figma.currentPage` must be switched before `createComponent()` — nodes can't be transferred across pages via `appendChild()`

## Scripter rules

See [`scripter.md`](scripter.md) for the full set of rules that prevent runtime crashes and silent failures when generating Figma Plugin API code. Highlights:

- Always load fonts before text operations
- `appendChild()` before `resize()` or layout properties
- Set `layoutMode` before any auto layout props
- Colors use 0-1 RGB, not hex strings
- Use `findOne()` for text overrides in instances (not `setProperties()`)

## License

MIT
