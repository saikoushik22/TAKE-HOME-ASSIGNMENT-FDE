"""Configuration layer.

Everything an evaluator might want to change — model, provider, retrieval tuning,
chunking behaviour — is settable from the environment. No application code should
ever need editing to switch a model. See architecture.md section 7.

Precedence: environment variable > .env file > the defaults below.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["ollama", "anthropic", "openai"]

# Repo root = backend/app/core/config.py -> up 4
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application settings, loaded once and cached."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_env: Literal["local", "docker", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ------------------------------------------------------------- database
    database_url: str = (
        "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_connect_retries: int = 5
    db_connect_backoff_seconds: float = 1.0

    # ------------------------------------------------------------ providers
    llm_provider: ProviderName = "ollama"
    llm_model: str = ""  # empty -> use the active provider's own default
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 180.0

    # Fallback is OFF by default. Silently answering with a different model than the
    # user selected makes results irreproducible, and a cloud fallback would ship
    # local data off the machine without consent. See architecture.md section 7.3.
    llm_fallback_enabled: bool = False
    llm_fallback_provider: ProviderName | None = None

    # ---------------------------------------------------------------- ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_keep_alive: str = "10m"

    # ------------------------------------------------------------- anthropic
    anthropic_api_key: str | None = None
    # Current model IDs carry no date suffix.
    anthropic_model: str = "claude-opus-5"

    # ---------------------------------------------------------------- openai
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str | None = None

    # ------------------------------------------------------------ embeddings
    # Embeddings stay local by default even when chat runs on a cloud provider:
    # re-embedding 30k chunks through a paid API is a cost surprise nobody asked for.
    embedding_provider: ProviderName = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    embedding_batch_size: int = 32

    # ------------------------------------------------------------------- rag
    rag_top_k: int = 8
    rag_candidates: int = 30
    # Calibrated against the golden set, not guessed. Measured cosine bands on
    # nomic-embed-text over this corpus:
    #     in-corpus      0.44 – 0.73  (median 0.62)
    #     out-of-corpus  0.43 – 0.55  (median 0.44)
    # The bands OVERLAP, so no threshold separates them perfectly — dense
    # embeddings keep a high similarity floor for any same-language text.
    # 0.50 rejects 7 of 8 known out-of-corpus questions while keeping 16 of 17
    # in-corpus ones. The residual case is caught downstream by the
    # retrieval-constrained prompt and citation validation, which together
    # produce an explicit refusal with zero citations (PRD R1, layers 1 and 3).
    rag_min_similarity: float = 0.50
    rag_max_per_episode: int = 3
    rag_rrf_k: int = 60

    # -------------------------------------------------------------- chunking
    chunk_target_chars: int = 1400
    chunk_max_chars: int = 2400
    chunk_overlap_turns: int = 1

    # -------------------------------------------------------------- ingestion
    transcript_repo: str = "ChatPRD/lennys-podcast-transcripts"
    transcript_ref: str = "main"
    transcript_local_path: str | None = None
    data_dir: str = str(REPO_ROOT / "data")
    ingest_max_episodes: int | None = None  # None = all; set low for a fast smoke test

    # ----------------------------------------------------------------- agent
    agent_runtime: Literal["native", "claude_sdk"] = "native"
    agent_max_steps: int = 4
    router_llm_fallback: bool = True

    # ------------------------------------------------------------- artifacts
    artifact_max_bytes: int = 512_000

    # ------------------------------------------------------------ validators
    @field_validator("llm_temperature")
    @classmethod
    def _temp_range(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("llm_temperature must be between 0.0 and 2.0")
        return v

    @field_validator("rag_min_similarity")
    @classmethod
    def _sim_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("rag_min_similarity must be between 0.0 and 1.0")
        return v

    @field_validator(
        "llm_fallback_provider",
        "openai_base_url",
        "transcript_local_path",
        "ingest_max_episodes",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        """Treat an empty environment variable as unset.

        `.env.example` ships optional settings as bare `KEY=` because that is
        how a reader expects to see "leave this blank". Without this, copying
        the example file verbatim — the documented setup path — fails startup
        with a type error, which is the worst possible first-run experience.

        Only nullable fields are listed. `llm_model` is deliberately absent: it
        is typed `str`, and its empty default already means "use the provider's
        own default", so coercing it to None here would fail type validation.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, v: object) -> object:
        """Treat an empty or placeholder key as absent.

        .env.example ships placeholders. Without this, a user who copies it
        verbatim gets a confusing 401 from the provider instead of a clear
        "not configured" state in the UI.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped or stripped.startswith(("your-", "sk-xxx", "changeme", "<")):
                return None
        return v

    @model_validator(mode="after")
    def _check_chunking(self) -> "Settings":
        if self.chunk_max_chars < self.chunk_target_chars:
            raise ValueError("chunk_max_chars must be >= chunk_target_chars")
        if self.rag_candidates < self.rag_top_k:
            raise ValueError("rag_candidates must be >= rag_top_k")
        return self

    # --------------------------------------------------------------- helpers
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def data_path(self) -> Path:
        """Where the corpus lives on disk.

        A relative DATA_DIR is resolved against the repository root, never the
        process working directory. `.env.example` ships `DATA_DIR=./data`, and
        the ingest CLI runs from `backend/` while the server runs from the repo
        root — without this, the same setting would point at two different
        directories and the corpus would silently be downloaded twice.
        """
        configured = Path(self.data_dir)
        if configured.is_absolute():
            return configured
        return (REPO_ROOT / configured).resolve()

    @property
    def transcripts_path(self) -> Path:
        if self.transcript_local_path:
            configured = Path(self.transcript_local_path)
            return configured if configured.is_absolute() else (REPO_ROOT / configured).resolve()
        return self.data_path / "transcripts"

    def model_for(self, provider: ProviderName) -> str:
        """Resolve the model name for a provider.

        An explicit LLM_MODEL wins, but only for the provider that is actually
        active — otherwise setting LLM_MODEL for Ollama would leak a local model
        name into an Anthropic request and produce a confusing 404.
        """
        if self.llm_model and provider == self.llm_provider:
            return self.llm_model
        return {
            "ollama": self.ollama_model,
            "anthropic": self.anthropic_model,
            "openai": self.openai_model,
        }[provider]

    def api_key_for(self, provider: ProviderName) -> str | None:
        return {
            "ollama": None,  # local, no key required
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
        }[provider]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Used as a FastAPI dependency."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read the environment. Used by tests."""
    get_settings.cache_clear()
    return get_settings()


def is_truthy(name: str, default: bool = False) -> bool:
    """Read a boolean env var outside the Settings object (used pre-startup)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
