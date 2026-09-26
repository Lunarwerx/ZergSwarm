"""Serve the web console against a SCRATCH home, for working on the console without touching your real settings.

    python scripts/console_dev.py [port]      # default 7815; home = tmp/console-home unless ZSWARM_HOME is set

Keys already in the environment or a clone's .secrets/ still show (masked); every change the page makes lands in
the scratch home. The real shared server on 7790 is left alone.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ZSWARM_HOME", str(ROOT / "tmp" / "console-home"))
sys.path.insert(0, str(ROOT))

from zswarm import console, shared  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7815
    # The scratch home's token, never the real one: this sign-in link is safe to print.
    print(f"zswarm console (scratch home {os.environ['ZSWARM_HOME']}): {console.sign_in_url(port)}", flush=True)
    shared.serve(port)
