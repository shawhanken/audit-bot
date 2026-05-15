"""Server entry point. Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from .api import build_app


app = build_app()
