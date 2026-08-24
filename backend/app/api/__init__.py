"""API router aggregation.

One place that composes the HTTP surface, so `main.py` mounts a single router
and the URL layout is readable in isolation.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import artifacts, chat, health, search, sessions

api_router = APIRouter(prefix="/api")

api_router.include_router(health.router)
api_router.include_router(sessions.router)
api_router.include_router(chat.router)
api_router.include_router(artifacts.router)
api_router.include_router(search.router)

__all__ = ["api_router"]
