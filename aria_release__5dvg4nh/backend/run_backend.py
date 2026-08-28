import argparse

from aria.config import get_settings
from aria.main import app

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run Local Agent backend")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    uvicorn.run(app, host=settings.http_host, port=args.port or settings.http_port)
