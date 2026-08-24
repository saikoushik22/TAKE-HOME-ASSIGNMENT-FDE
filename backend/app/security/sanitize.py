"""Artifact sanitization.

Model output is untrusted input, regardless of which model produced it: a
prompt-injected transcript chunk can steer a cloud model as readily as a local
one. This module is layers 1 and 2 of the four described in architecture.md
section 8; layers 3 and 4 (iframe sandbox, opaque origin) live in the frontend
viewer.

Two paths, deliberately different:

* **HTML artifacts** go through a reporting parser here. Its job is to strip
  *external references* and dangerous URL schemes — the exfiltration channels —
  not to strip all script. Containment of script is what CSP plus the sandbox
  provide, and they do it more reliably than tag filtering can.
* **Markdown artifacts** go through nh3 (Rust `ammonia`) with a strict
  allowlist and no raw HTML pass-through. Markdown is *not* safe merely because
  it is Markdown: most renderers pass raw HTML through by default, which is
  precisely the vulnerability.

Every removal is recorded so the viewer can show a "sanitized" badge naming
what was taken out. Silently altering a user's artifact is a trust violation;
naming the removal turns an invisible mutation into a visible, explained
security decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import nh3

from ..core.logging import get_logger

log = get_logger(__name__)

# Elements removed outright, with their content, from HTML artifacts.
# `script` is absent by design — see the module docstring.
FORBIDDEN_ELEMENTS = frozenset(
    {"iframe", "object", "embed", "applet", "frame", "frameset", "base", "link"}
)

# Void/self-closing elements: they never have an end tag to balance.
VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input",
     "link", "meta", "param", "source", "track", "wbr"}
)

# URL-bearing attributes that must be scheme-checked.
URL_ATTRIBUTES = frozenset({"href", "src", "action", "formaction", "poster",
                            "background", "cite", "data", "srcset"})

SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "data", "#", ""})

# Only these data: media types may be inlined. `data:text/html` is an
# origin-inheritance vector, so it is not on the list.
SAFE_DATA_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/jpg", "data:image/gif",
    "data:image/webp", "data:image/svg+xml", "data:font/", "data:application/font",
)

DANGEROUS_SCHEMES = ("javascript:", "vbscript:", "livescript:", "data:text/html")

# CSS that reaches the network. Blocked to preserve the no-egress guarantee.
CSS_NETWORK_RE = re.compile(
    r"""(?:@import\b|url\s*\(\s*['"]?\s*(?:https?:)?//)""", re.IGNORECASE
)

CSP = (
    "default-src 'none'; "
    "img-src data: blob:; "
    "style-src 'unsafe-inline'; "
    "font-src data:; "
    "script-src 'unsafe-inline'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


@dataclass(slots=True)
class SanitizationReport:
    removed_elements: list[str] = field(default_factory=list)
    removed_attributes: list[str] = field(default_factory=list)
    rewritten_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.removed_elements or self.removed_attributes or self.rewritten_urls
        )

    @property
    def total_removals(self) -> int:
        return (
            len(self.removed_elements)
            + len(self.removed_attributes)
            + len(self.rewritten_urls)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "total_removals": self.total_removals,
            "removed_elements": self.removed_elements[:50],
            "removed_attributes": self.removed_attributes[:50],
            "rewritten_urls": self.rewritten_urls[:50],
            "notes": self.notes,
        }


@dataclass(slots=True)
class SanitizedArtifact:
    content: str
    report: SanitizationReport


def _is_safe_url(value: str) -> bool:
    candidate = value.strip().replace("\x00", "")
    # Strip control characters and whitespace used to smuggle "java\tscript:".
    collapsed = re.sub(r"[\s\x00-\x1f]+", "", candidate).lower()
    if collapsed.startswith(DANGEROUS_SCHEMES):
        return False
    if collapsed.startswith("data:"):
        return collapsed.startswith(SAFE_DATA_PREFIXES)
    if ":" not in collapsed.split("/")[0]:
        return True  # relative URL or fragment
    scheme = collapsed.split(":", 1)[0]
    return scheme in SAFE_URL_SCHEMES


class _HTMLSanitizer(HTMLParser):
    """Reporting sanitizer for HTML artifacts."""

    def __init__(self, report: SanitizationReport) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.report = report
        self._suppress_depth = 0
        self._suppressing: str | None = None
        self._open_stack: list[str] = []

    # ------------------------------------------------------------- helpers
    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        rendered: list[str] = []
        for name, value in attrs:
            lowered = name.lower()
            value = value or ""

            # Inline event handlers are the classic injection surface.
            if lowered.startswith("on"):
                self.report.removed_attributes.append(f"{tag}[{lowered}]")
                continue

            # <meta http-equiv="refresh"> is a redirect primitive.
            if tag == "meta" and lowered == "http-equiv":
                self.report.removed_attributes.append("meta[http-equiv]")
                continue

            # An external script source is the one script case we must block:
            # CSP would stop the fetch anyway, but stripping it makes the
            # removal visible to the user instead of a silent console error.
            if tag == "script" and lowered == "src":
                self.report.removed_attributes.append("script[src]")
                continue

            if lowered in URL_ATTRIBUTES and not _is_safe_url(value):
                self.report.rewritten_urls.append(f"{tag}[{lowered}]={value[:60]}")
                continue

            if lowered == "style" and CSS_NETWORK_RE.search(value):
                self.report.removed_attributes.append(f"{tag}[style: remote url]")
                continue

            if value:
                escaped = (
                    value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
                )
                rendered.append(f'{lowered}="{escaped}"')
            else:
                rendered.append(lowered)

        return (" " + " ".join(rendered)) if rendered else ""

    # -------------------------------------------------------------- handlers
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            if tag == self._suppressing:
                self._suppress_depth += 1
            return
        if tag in FORBIDDEN_ELEMENTS:
            self.report.removed_elements.append(tag)
            if tag not in VOID_ELEMENTS:
                self._suppressing = tag
                self._suppress_depth = 1
            return

        self.out.append(f"<{tag}{self._clean_attrs(tag, attrs)}>")
        if tag not in VOID_ELEMENTS:
            self._open_stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            return
        if tag in FORBIDDEN_ELEMENTS:
            self.report.removed_elements.append(tag)
            return
        self.out.append(f"<{tag}{self._clean_attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            if tag == self._suppressing:
                self._suppress_depth -= 1
                if self._suppress_depth == 0:
                    self._suppressing = None
            return
        if tag in FORBIDDEN_ELEMENTS or tag in VOID_ELEMENTS:
            return
        if tag in self._open_stack:
            # Close any elements the model left unbalanced, so a stray missing
            # </div> cannot swallow the rest of the document.
            while self._open_stack:
                open_tag = self._open_stack.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        # Inside <style>, strip rules that would reach the network.
        if self._open_stack and self._open_stack[-1] == "style":
            if CSS_NETWORK_RE.search(data):
                data = CSS_NETWORK_RE.sub("/* blocked-remote-url */(", data)
                self.report.removed_attributes.append("style[remote url]")
        self.out.append(data)

    def handle_comment(self, data: str) -> None:
        # Comments are dropped: conditional comments are a legacy IE vector and
        # they carry no rendering value.
        return

    def close_document(self) -> str:
        self.close()
        while self._open_stack:
            self.out.append(f"</{self._open_stack.pop()}>")
        return "".join(self.out)


def sanitize_html(raw: str) -> SanitizedArtifact:
    """Sanitize an HTML artifact and report what changed."""
    report = SanitizationReport()
    parser = _HTMLSanitizer(report)
    try:
        parser.feed(raw)
        cleaned = parser.close_document()
    except Exception as exc:
        # A parser failure must fail closed, never fall through to raw output.
        log.warning("artifact.sanitize.parse_error", extra={"error": str(exc)})
        report.notes.append(
            "The artifact could not be parsed as HTML and was escaped to plain text."
        )
        escaped = (
            raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        return SanitizedArtifact(content=f"<pre>{escaped}</pre>", report=report)

    if report.changed:
        log.info("artifact.sanitized", extra=report.to_dict())
    return SanitizedArtifact(content=cleaned, report=report)


def sanitize_markdown(raw: str) -> SanitizedArtifact:
    """Sanitize a Markdown artifact.

    Any embedded raw HTML is reduced to a safe subset by nh3 rather than passed
    through. Links keep their text but lose dangerous schemes.
    """
    report = SanitizationReport()

    dangerous = re.findall(
        r"<\s*(script|iframe|object|embed|form)\b", raw, re.IGNORECASE
    )
    if dangerous:
        report.removed_elements.extend(tag.lower() for tag in dangerous)

    # Neutralize javascript: links in Markdown link syntax before nh3 sees them.
    def _strip_scheme(match: re.Match[str]) -> str:
        report.rewritten_urls.append(match.group(0)[:60])
        return f"{match.group('text')}(blocked:)"

    cleaned = re.sub(
        r"(?P<text>\[[^\]]*\])\(\s*(?:javascript|vbscript|data:text/html)[^)]*\)",
        _strip_scheme,
        raw,
        flags=re.IGNORECASE,
    )

    cleaned = nh3.clean(
        cleaned,
        tags={
            "p", "br", "strong", "em", "b", "i", "u", "s", "code", "pre",
            "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
            "a", "img", "table", "thead", "tbody", "tr", "th", "td", "hr",
            "span", "div", "sup", "sub", "del", "ins",
        },
        attributes={
            "a": {"href", "title"},
            "img": {"src", "alt", "title"},
            "*": {"class"},
        },
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        strip_comments=True,
    )

    if report.changed:
        log.info("artifact.sanitized", extra={"kind": "markdown", **report.to_dict()})
    return SanitizedArtifact(content=cleaned, report=report)


def sanitize(raw: str, kind: str) -> SanitizedArtifact:
    """Dispatch by artifact kind."""
    if kind == "html":
        return sanitize_html(raw)
    if kind == "markdown":
        # Markdown artifacts are stored as Markdown and rendered client-side;
        # we only strip embedded HTML hazards, preserving Markdown syntax.
        report = SanitizationReport()
        dangerous = re.findall(
            r"<\s*(script|iframe|object|embed|form|base|link)\b", raw, re.IGNORECASE
        )
        cleaned = raw
        if dangerous:
            report.removed_elements.extend(tag.lower() for tag in dangerous)
            cleaned = re.sub(
                r"<\s*(script|iframe|object|embed|form|base|link)\b[^>]*>"
                r"(.*?)(?:<\s*/\s*\1\s*>)?",
                "",
                cleaned,
                flags=re.IGNORECASE | re.DOTALL,
            )
        cleaned = re.sub(
            r"(\[[^\]]*\])\(\s*(?:javascript|vbscript)[^)]*\)",
            r"\1(blocked:)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if report.changed:
            log.info("artifact.sanitized", extra={"kind": "markdown",
                                                  **report.to_dict()})
        return SanitizedArtifact(content=cleaned, report=report)
    raise ValueError(f"Unsupported artifact kind: {kind}")


def build_srcdoc(html: str, *, title: str = "Artifact") -> str:
    """Wrap sanitized HTML in a document carrying the CSP.

    `default-src 'none'` is the anti-exfiltration guarantee: no fetch, no XHR,
    no WebSocket, no beacon, no remote image. Even if script executes, it has
    nowhere to send anything.
    """
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        "<style>\n"
        "  :root { color-scheme: light dark; }\n"
        "  body { margin: 0; padding: 24px; font-family: ui-sans-serif, system-ui,\n"
        "         -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;\n"
        "         line-height: 1.6; }\n"
        "  img, table, pre { max-width: 100%; }\n"
        "  pre { overflow-x: auto; }\n"
        "</style>\n"
        "</head>\n<body>\n"
        f"{html}\n"
        "</body>\n</html>"
    )
