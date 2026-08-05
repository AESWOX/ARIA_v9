#!/usr/bin/env python
"""ARIA v8 start — гарантированно загружает .env и запускает uvicorn."""
import os
import sys

# Load .env (уровень выше run_aria.py)
env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
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
