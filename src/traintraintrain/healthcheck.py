"""Container health check: `python -m traintraintrain.healthcheck`.

Healthy when the web UI answers /healthz. A container running a one-off `depart` job has no
server to probe, so it always reports healthy; the job's exit code is what matters there.
"""
from __future__ import annotations

import sys
import urllib.request

from ._compat import env


def main() -> int:
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            args = fh.read().split(b"\0")
    except OSError:
        args = []
    if b"ui" not in args:
        return 0
    port = env("UI_PORT", "8080")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=4) as resp:
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
