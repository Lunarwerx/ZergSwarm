"""Serve ~/.zswarm over http so the report page can be opened in a browser that will not read file:// URLs
(the Claude desktop preview pane is one). Read-only, localhost only, no dependencies.

    python scripts/serve_report.py [port]     ->  http://127.0.0.1:8731/zswarm.html
"""
from __future__ import annotations

import functools
import http.server
import socketserver
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zswarm import config  # noqa: E402


def main(argv: list[str]) -> int:
    port = int(argv[0]) if argv else 8731
    root = config.HOME
    root.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {root} at http://127.0.0.1:{port}/zswarm.html", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
