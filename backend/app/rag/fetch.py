"""Corpus acquisition.

The transcripts are third-party content, so this repository does not vendor
them. They are fetched at setup time instead, which also keeps the repo small
and makes a corpus refresh a first-class operation rather than a commit.

A tarball of the default branch is used rather than `git clone`: it avoids
requiring git inside the container and skips history we have no use for.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

import httpx

from ..core.errors import AppError
from ..core.logging import get_logger

log = get_logger(__name__)

TARBALL_URL = "https://codeload.github.com/{repo}/tar.gz/refs/heads/{ref}"


class CorpusFetchError(AppError):
    code = "CORPUS_FETCH_FAILED"
    status_code = 503
    hint = (
        "Check network access to github.com. For an air-gapped run, download the "
        "repository manually and set TRANSCRIPT_LOCAL_PATH to the episodes directory."
    )


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract, refusing any member that would escape the destination.

    A tarball is untrusted input. Without this check a crafted archive could
    write outside the target directory via `../` members or symlinks.
    """
    destination = destination.resolve()
    for member in tar.getmembers():
        target = (destination / member.name).resolve()
        if not target.is_relative_to(destination):
            raise CorpusFetchError(
                f"Refusing to extract member outside the target directory: {member.name}"
            )
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if not link_target.is_relative_to(destination):
                raise CorpusFetchError(
                    f"Refusing to extract link escaping the target: {member.name}"
                )
    tar.extractall(destination)


def fetch_corpus(
    *,
    repo: str,
    ref: str,
    destination: Path,
    force: bool = False,
    timeout: float = 300.0,
) -> Path:
    """Download and extract the transcript corpus. Returns the episodes root."""
    destination = Path(destination)
    episodes_dir = destination / "episodes"

    if episodes_dir.exists() and any(episodes_dir.iterdir()) and not force:
        log.info(
            "corpus.fetch.skipped",
            extra={"reason": "already present", "path": str(episodes_dir)},
        )
        return episodes_dir

    url = TARBALL_URL.format(repo=repo, ref=ref)
    log.info("corpus.fetch.start", extra={"repo": repo, "ref": ref, "url": url})

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.content
    except httpx.HTTPError as exc:
        raise CorpusFetchError(
            f"Could not download the transcript corpus from {repo}@{ref}",
            detail={"repo": repo, "ref": ref, "error": str(exc)},
        ) from exc

    staging = destination / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            _safe_extract(tar, staging)
    except tarfile.TarError as exc:
        raise CorpusFetchError(
            "The downloaded corpus archive could not be extracted",
            detail={"error": str(exc)},
        ) from exc

    # The archive contains a single top-level directory, e.g. "<repo>-main/".
    roots = [p for p in staging.iterdir() if p.is_dir()]
    if not roots:
        raise CorpusFetchError("The corpus archive was empty")
    extracted_episodes = roots[0] / "episodes"
    if not extracted_episodes.exists():
        raise CorpusFetchError(
            "The corpus archive has no 'episodes' directory",
            detail={"found": [p.name for p in roots[0].iterdir()][:20]},
        )

    if episodes_dir.exists():
        shutil.rmtree(episodes_dir)
    episodes_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(extracted_episodes), str(episodes_dir))

    # The curated topic index is small and useful for corpus-coverage answers.
    index_src = roots[0] / "index"
    if index_src.exists():
        index_dst = destination / "index"
        if index_dst.exists():
            shutil.rmtree(index_dst)
        shutil.move(str(index_src), str(index_dst))

    shutil.rmtree(staging, ignore_errors=True)

    count = sum(1 for _ in episodes_dir.rglob("transcript.md"))
    log.info(
        "corpus.fetch.complete",
        extra={"episodes": count, "path": str(episodes_dir),
               "bytes": len(payload)},
    )
    return episodes_dir
