"""Request and response contracts.

Pydantic v2 models define the API surface. They import from nothing else in the
application — no database rows, no domain objects — so the wire format can never
drift into an accidental leak of an internal shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SkillName = Literal["grounded_qa", "ship30_essay", "artifact"]
SkillOverride = Literal[
    "grounded_qa", "ship30_essay", "artifact", "artifact_markdown", "artifact_html"
]
ProviderName = Literal["ollama", "anthropic", "openai"]
ArtifactKind = Literal["markdown", "html"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ============================================================== sessions


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    provider: ProviderName | None = None
    model: str | None = Field(default=None, max_length=120)
    user_id: str | None = Field(default=None, max_length=200)
    user_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user_metadata")
    @classmethod
    def _bounded_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Unbounded client-supplied JSON in a database column is how a chat app
        # becomes an object store. 16 keys is generous for provenance metadata.
        if len(v) > 16:
            raise ValueError("user_metadata may contain at most 16 keys")
        return v


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    provider: ProviderName | None = None
    model: str | None = Field(default=None, max_length=120)


class SessionSummary(ORMModel):
    id: uuid.UUID
    title: str
    provider: str
    model: str
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    index: int
    chunk_id: str | None = None
    episode_id: str | None = None
    episode_title: str | None = None
    guest: str | None = None
    speakers: list[str] = Field(default_factory=list)
    timestamp: str | None = None
    start_seconds: int | None = None
    url: str | None = None
    snippet: str | None = None
    score: float | None = None


class MessageOut(ORMModel):
    id: uuid.UUID
    session_id: uuid.UUID
    seq: int
    role: str
    content: str
    skill: str | None = None
    provider: str | None = None
    model: str | None = None
    citations: list[Citation] | None = None
    retrieval_trace: dict[str, Any] | None = None
    token_usage: dict[str, Any] | None = None
    latency_ms: int | None = None
    created_at: datetime


class SessionDetail(ORMModel):
    id: uuid.UUID
    title: str
    provider: str
    model: str
    user_id: str | None = None
    user_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut] = Field(default_factory=list)


class SessionList(BaseModel):
    sessions: list[SessionSummary]
    total: int


# ================================================================== chat


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    skill: SkillOverride | None = Field(
        default=None,
        description="Force a skill instead of routing. Used by UI action buttons.",
    )

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped


class ArtifactOut(ORMModel):
    id: uuid.UUID
    session_id: uuid.UUID
    message_id: uuid.UUID | None = None
    kind: ArtifactKind
    title: str
    content: str
    sanitization_report: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ArtifactList(BaseModel):
    artifacts: list[ArtifactOut]


class ChatResponse(BaseModel):
    """Buffered (non-streaming) turn result."""

    session_id: uuid.UUID
    message_id: uuid.UUID
    user_message_id: uuid.UUID
    content: str
    skill: str
    provider: str
    model: str
    citations: list[Citation] = Field(default_factory=list)
    artifact: ArtifactOut | None = None
    abstained: bool = False
    latency_ms: int
    fallback_from: str | None = None


# ================================================================ search


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int | None = Field(default=None, ge=1, le=50)


class SearchHit(BaseModel):
    chunk_id: str
    episode_id: str
    episode_title: str
    guest: str | None = None
    timestamp: str | None = None
    url: str | None = None
    text: str
    vector_similarity: float
    lexical_rank: float
    fused_score: float
    matched_by: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    abstained: bool
    reason: str | None = None
    candidates_considered: int
    hits: list[SearchHit] = Field(default_factory=list)
    took_ms: int


# ============================================================ health/config


class DependencyStatus(BaseModel):
    name: str
    healthy: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    dependencies: list[DependencyStatus]
    corpus: dict[str, Any] = Field(default_factory=dict)


class ProviderInfo(BaseModel):
    name: str
    available: bool
    model: str
    reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ConfigResponse(BaseModel):
    """Everything the UI needs to render itself correctly on first paint."""

    active_provider: str
    active_model: str
    providers: list[ProviderInfo]
    embedding_provider: str
    embedding_model: str
    fallback_enabled: bool
    fallback_provider: str | None = None
    skills: list[str]
    corpus: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    """The single error envelope every non-2xx response uses."""

    error: ErrorBody
