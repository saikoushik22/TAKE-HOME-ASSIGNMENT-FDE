"""FastAPI dependencies.

Long-lived objects — the engine, the provider registry — are built once during
lifespan and hang off ``app.state``. These helpers read them back out, so no
module reaches for a global singleton and tests can swap the whole graph by
overriding a dependency.
"""

from __future__ import annotations

from typing import Annotated, AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.session import Database
from ..llm.registry import ProviderRegistry


def get_db_handle(request: Request) -> Database:
    return request.app.state.database


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a transactional database session for one request."""
    database: Database = request.app.state.database
    async for session in database.session():
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
DatabaseDep = Annotated[Database, Depends(get_db_handle)]
