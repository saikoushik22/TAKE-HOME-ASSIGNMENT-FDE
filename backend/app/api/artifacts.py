"""Artifact retrieval.

The stored `content` column is already sanitized (the orchestrator sanitizes on
the way in), so every read path inherits that guarantee. `raw_content` is never
exposed through the API — it exists only for audit via direct database access.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response

from ..db.repositories import ArtifactRepository
from ..schemas import ArtifactOut
from ..security.sanitize import build_srcdoc
from .deps import DbDep

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}", response_model=ArtifactOut, summary="Fetch one artifact")
async def get_artifact(artifact_id: uuid.UUID, db: DbDep) -> ArtifactOut:
    row = await ArtifactRepository(db).get(artifact_id)
    return ArtifactOut(**row)


@router.get(
    "/{artifact_id}/srcdoc",
    response_class=Response,
    summary="Sanitized HTML document, CSP-wrapped, for iframe rendering",
)
async def get_artifact_srcdoc(artifact_id: uuid.UUID, db: DbDep) -> Response:
    """Return the full document the viewer renders.

    Offered as an endpoint so a reviewer can inspect exactly what the iframe is
    given — CSP header included — with a single curl, rather than having to
    reconstruct it from the client bundle.

    Served as text/plain deliberately: this endpoint is for reading, not for
    navigating to. Serving it as text/html would create a same-origin page that
    executes the artifact's script with the app's origin — precisely the
    property the sandboxed iframe exists to deny.
    """
    row = await ArtifactRepository(db).get(artifact_id)
    document = (
        build_srcdoc(row["content"], title=row["title"])
        if row["kind"] == "html"
        else row["content"]
    )
    return Response(
        content=document,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
