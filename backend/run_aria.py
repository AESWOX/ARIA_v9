#!/usr/bin/env python
"""ARIA v9 start — гарантированно загружает .env и запускает uvicorn."""
import os
import sys

# PyInstaller console=False: sys.stdout/sys.stderr are None, and uvicorn's log
# formatter then fails with "Unable to configure formatter 'default'" — the
# sidecar would die on boot. Give it a null sink (real logs go to backend.log).
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

# Load .env (уровень выше run_aria.py)
from aria import paths as _paths  # noqa: E402

env_file = str(_paths.env_file())
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val:
                os.environ.setdefault(key, val)

# --- parse --port ---
port = None
if '--port' in sys.argv:
    idx = sys.argv.index('--port')
    if idx + 1 < len(sys.argv):
        port = int(sys.argv[idx + 1])

if port is None:
    port_str = os.environ.get('HTTP_PORT', '8765').strip()
    port = int(port_str) if port_str else 8765

host = os.environ.get('HTTP_HOST', '127.0.0.1').strip() or '127.0.0.1'

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(
        'aria.main:app',
        host=host,
        port=port,
        log_level='info',
    )
