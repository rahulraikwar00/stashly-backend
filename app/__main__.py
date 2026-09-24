"""Run the backend with the port from .env (`PORT`, default 8000).

Usage:
    cd backend
    .venv/bin/python -m app
"""

from __future__ import annotations

import uvicorn

from .config import load_settings
from .main import app


def main() -> None:
    settings = load_settings()
    print(f"Bookmark Backend — serving on http://0.0.0.0:{settings.port}")
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="info")


if __name__ == "__main__":
    main()