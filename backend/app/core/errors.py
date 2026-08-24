"""Structured error types.

Every error carries a machine-readable ``code``, a human ``message``, an
actionable ``hint``, and the correlation ID that ties it to the server logs.

The ``hint`` field is the one that earns its keep: the person who hits an error
at 11pm is usually not the person who wrote the code, and "Ollama is not
reachable" is only half an answer without "start it with `ollama serve`".
"""

from __future__ import annotations

from typing import Any

from .logging import correlation_id_var


class AppError(Exception):
    """Base class for all errors this application raises deliberately.

    Anything that is *not* an AppError is an unexpected bug, and the API layer
    treats it as such: 500, generic message, full traceback in the logs only.
    """

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        detail: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        if hint is not None:
            self.hint = hint
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        detail = dict(self.detail)
        if self.hint:
            detail["hint"] = self.hint
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": detail,
                "correlation_id": correlation_id_var.get(),
            }
        }


# --------------------------------------------------------------------- 4xx


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status_code = 422


class PayloadTooLargeError(AppError):
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413


# --------------------------------------------------------------------- 5xx


class ProviderUnavailableError(AppError):
    """A model provider is not configured, not reachable, or refused the call."""

    code = "PROVIDER_UNAVAILABLE"
    status_code = 503


class ProviderTimeoutError(AppError):
    code = "PROVIDER_TIMEOUT"
    status_code = 504
    hint = (
        "The model took too long to respond. Increase LLM_TIMEOUT_SECONDS, or "
        "switch to a smaller model such as llama3.2:3b."
    )


class DatabaseUnavailableError(AppError):
    code = "DATABASE_UNAVAILABLE"
    status_code = 503
    hint = (
        "PostgreSQL is not reachable. Check `docker compose ps` and confirm "
        "DATABASE_URL points at a running instance."
    )


class CorpusEmptyError(AppError):
    """The knowledge base has not been ingested.

    Distinct from "retrieval found nothing": an empty corpus is an operator
    problem with a known fix, while empty retrieval is a legitimate product
    state. Conflating them would send the user chasing the wrong thing.
    """

    code = "CORPUS_EMPTY"
    status_code = 503
    hint = "The transcript corpus is empty. Run `make ingest` to populate it."


class SkillExecutionError(AppError):
    code = "SKILL_EXECUTION_ERROR"
    status_code = 500


class ArtifactRenderError(AppError):
    code = "ARTIFACT_RENDER_ERROR"
    status_code = 500
