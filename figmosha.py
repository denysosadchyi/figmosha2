#!/usr/bin/env python3
"""figmosha — CLI client for the Figmosha 2.0 bridge.

Usage:
    figmosha "figma.notify('hi')"        # inline code
    figmosha --file script.js             # from a file
    figmosha --stdin < script.js          # from stdin
    figmosha --status                     # check server / plugin
    figmosha --raw "return ..."           # print full JSON response

Sends `code` to the bridge at http://localhost:8787/exec.
Prints result to stdout; logs and timing to stderr.
Exit code: 0 success, 1 plugin error, 2 transport error.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def _request(method: str, path: str, payload=None, timeout: int = 65, host: str = "localhost", port: int = 8787):
    url = f"http://{host}:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"ok": False, "error": body.decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        return 0, {"ok": False, "error": f"connection: {e.reason}"}
    except TimeoutError:
        return 0, {"ok": False, "error": "request timed out"}


def main():
    ap = argparse.ArgumentParser(prog="figmosha", description=__doc__.splitlines()[0])
    ap.add_argument("code", nargs="?", help="inline JS code")
    ap.add_argument("--file", "-f", help="JS file to send")
    ap.add_argument("--stdin", action="store_true", help="read code from stdin")
    ap.add_argument("--status", action="store_true", help="GET /status")
    ap.add_argument("--timeout", "-t", type=int, default=60, help="plugin-side timeout (s)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--raw", action="store_true", help="print full JSON response")
    args = ap.parse_args()

    if args.status:
        status, resp = _request("GET", "/status", host=args.host, port=args.port)
        print(json.dumps(resp, indent=2))
        sys.exit(0 if status == 200 else 2)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            code = f.read()
    elif args.stdin:
        code = sys.stdin.read()
    elif args.code:
        code = args.code
    else:
        ap.print_help()
        sys.exit(2)

    status, resp = _request(
        "POST", "/exec",
        payload={"code": code, "timeout": args.timeout},
        timeout=args.timeout + 5,
        host=args.host, port=args.port,
    )

    if args.raw:
        print(json.dumps(resp, indent=2))
        sys.exit(0 if resp.get("ok") else 1)

    if status == 0:
        print(f"figmosha: {resp.get('error', 'unknown transport error')}", file=sys.stderr)
        sys.exit(2)

    for line in resp.get("logs") or []:
        print(f"  log: {line}", file=sys.stderr)

    if resp.get("ok") is False:
        print(f"figmosha: {resp.get('error', 'unknown')}", file=sys.stderr)
        if resp.get("stack"):
            print(resp["stack"], file=sys.stderr)
        sys.exit(1)

    if resp.get("result"):
        print(resp["result"])
    print(f"  ({resp.get('elapsed_ms', '?')}ms)", file=sys.stderr)


if __name__ == "__main__":
    main()
