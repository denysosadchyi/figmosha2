#!/usr/bin/env python3
"""Execute Figma Plugin API code via Scripter.

Modes:
    python run.py --serve URL     # start browser, login, open file, wait for commands
    python run.py "code"          # send code to running server via fifo
    python run.py --file script.js

Server writes result screenshot to result.png after each execution.
Communication via /tmp/figmosha.fifo (named pipe).
"""

import os
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

DIR = Path(__file__).parent
STATE_PATH = DIR / ".auth-state.json"
SCREENSHOT_PATH = DIR / "result.png"
FIFO_PATH = Path("/tmp/figmosha.fifo")


async def scripter_exec(page, code: str):
    """Paste and run code in Scripter."""
    f4 = (
        page.frame_locator('iframe[title="Plugin: Scripter"]')
        .frame_locator('iframe[name="Network Plugin Iframe"]')
        .frame_locator('iframe[name="Inner Plugin Iframe"]')
        .frame_locator('#iframe0')
    )
    await f4.locator('[title="New script"]').click(force=True)
    await page.wait_for_timeout(500)
    await f4.locator('.view-lines').click(force=True)
    await page.wait_for_timeout(300)
    await page.evaluate("(t) => navigator.clipboard.writeText(t)", code)
    await page.keyboard.press("Control+v")
    await page.wait_for_timeout(300)
    await f4.get_by_title("Run  (Ctrl+Return)").click()
    await page.wait_for_timeout(2000)
    await page.screenshot(path=str(SCREENSHOT_PATH), scale="css", type="png")

    # Read Scripter output (print() results)
    try:
        output_el = f4.locator('.output-lines, [class*=output], [class*=message]')
        count = await output_el.count()
        if count > 0:
            text = await output_el.first.inner_text()
            if text.strip():
                output_path = DIR / "output.txt"
                output_path.write_text(text.strip())
    except Exception:
        pass


async def serve(url: str, email: str = None, password: str = None):
    """Long-running server: open Figma, listen for code on fifo."""
    # Create fifo
    if FIFO_PATH.exists():
        FIFO_PATH.unlink()
    os.mkfifo(FIFO_PATH)

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=False)
        if STATE_PATH.exists():
            ctx = await browser.new_context(storage_state=str(STATE_PATH))
        else:
            ctx = await browser.new_context()

        page = await ctx.new_page()

        # Login if needed
        if not STATE_PATH.exists() and email and password:
            await page.goto("https://www.figma.com/login")
            await page.wait_for_timeout(3000)
            await page.get_by_role("textbox", name="Email").fill(email)
            await page.get_by_role("textbox", name="Password").fill(password)
            await page.get_by_role("button", name="Log in").click()
            await page.wait_for_timeout(5000)
            await ctx.storage_state(path=str(STATE_PATH))
            print("Logged in, session saved.")

        await page.goto(url)
        await page.wait_for_timeout(5000)

        # Open Scripter via Quick Actions with retry
        for attempt in range(3):
            await page.keyboard.press("Control+/")
            await page.wait_for_timeout(1000)
            await page.keyboard.type("Scripter", delay=50)
            await page.wait_for_timeout(1000)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)

            # Check if Scripter iframe appeared
            scripter = page.frame_locator('iframe[title="Plugin: Scripter"]')
            try:
                await scripter.locator("body").wait_for(timeout=5000)
                print("Scripter opened.")
                break
            except Exception:
                print(f"Scripter not found (attempt {attempt + 1}), retrying...")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1000)
        else:
            print("Error: could not open Scripter after 3 attempts", file=sys.stderr)
            await browser.close()
            return

        print(f"Ready. Send code to {FIFO_PATH}")
        print(f"  echo 'figma code' > {FIFO_PATH}")

        # Listen for commands
        while True:
            with open(FIFO_PATH, 'r') as fifo:
                code = fifo.read().strip()
            if not code:
                continue
            if code == "__quit__":
                break
            try:
                await scripter_exec(page, code)
                print(f"OK → {SCREENSHOT_PATH}")
            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)

        await browser.close()
    FIFO_PATH.unlink(missing_ok=True)


def send_code(code: str):
    """Send code to running server via fifo."""
    if not FIFO_PATH.exists():
        print("Error: server not running. Start with: python run.py --serve URL", file=sys.stderr)
        sys.exit(1)
    with open(FIFO_PATH, 'w') as f:
        f.write(code)
    print(f"Sent. Check {SCREENSHOT_PATH}")


def main():
    if "--serve" in sys.argv:
        idx = sys.argv.index("--serve")
        url = sys.argv[idx + 1]
        email = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else None
        password = sys.argv[idx + 3] if len(sys.argv) > idx + 3 else None
        asyncio.run(serve(url, email, password))
    elif "--file" in sys.argv:
        idx = sys.argv.index("--file")
        with open(sys.argv[idx + 1]) as f:
            send_code(f.read().strip())
    elif len(sys.argv) > 1:
        send_code(sys.argv[1])
    else:
        print("Usage:")
        print("  python run.py --serve FIGMA_URL   # start server")
        print('  python run.py "code"              # send code')
        print("  python run.py --file script.js    # send from file")


if __name__ == "__main__":
    main()
