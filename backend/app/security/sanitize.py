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
* **Markdown artifacts** are stored as Markdown, so they are cleaned with
  syntax-preserving passes that delete dangerous elements *together with their
  bodies*. An HTML sanitizer is deliberately NOT used here: running one over
  Markdown source HTML-escapes the plain text, turning `> quote` into
  `&gt; quote` and destroying every blockquote. Markdown is *not* safe merely
  because it is Markdown — most renderers pass raw HTML through by default,
  which is precisely the vulnerability — so the stored content is cleaned here
  and sanitized again at render time by DOMPurify.

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
# Detector only — used to decide whether a style needs rewriting at all.
CSS_NETWORK_RE = re.compile(
    r"""(?:@import\b|url\s*\(\s*['"]?\s*(?:https?:)?//)""", re.IGNORECASE
)

# Substitution patterns. These consume the ENTIRE construct, not just its
# prefix: replacing only `url(` leaves the host sitting in stored content, so
# the sanitization report would claim a removal that did not happen.
CSS_IMPORT_SUB_RE = re.compile(r"@import\b[^;]*;?", re.IGNORECASE)
CSS_URL_SUB_RE = re.compile(
    r"""url\s*\(\s*['"]?\s*(?:https?:)?//[^)]*\)""", re.IGNORECASE
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
                data = CSS_IMPORT_SUB_RE.sub("/* blocked-remote-import */", data)
                data = CSS_URL_SUB_RE.sub("url(about:blank)", data)
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


# Elements stripped from Markdown *together with their content*. An element
# whose body is executable or fetchable is not made safe by dropping its tags:
# removing only `<script>` leaves `alert(1)` sitting in the document as text
# that the next renderer may happily re-parse.
_MD_DANGEROUS = "script|style|iframe|object|embed|applet|form|frame|frameset"

# Paired form: opening tag through matching closing tag, body included.
_MD_PAIRED_RE = re.compile(
    rf"<\s*({_MD_DANGEROUS})\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Unclosed form: a model can emit `<script>` with no terminator. Fail closed by
# deleting to end of document rather than leaving the body behind.
_MD_UNCLOSED_RE = re.compile(
    rf"<\s*({_MD_DANGEROUS})\b[^>]*>.*", re.IGNORECASE | re.DOTALL
)

# Void elements that carry no body but do carry a hazard.
_MD_VOID_RE = re.compile(
    r"<\s*(base|link|meta)\b[^>]*>", re.IGNORECASE
)

# Inline event handlers on any surviving raw HTML.
_MD_EVENT_RE = re.compile(r"\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)

# Dangerous schemes in Markdown link/image syntax: [text](javascript:…)
_MD_LINK_SCHEME_RE = re.compile(
    r"(?P<text>!?\[[^\]]*\])\(\s*(?:javascript|vbscript|livescript|data:text/html)[^)]*\)",
    re.IGNORECASE,
)

# Dangerous schemes in raw href/src attributes that survived in inline HTML.
_MD_ATTR_SCHEME_RE = re.compile(
    r"""(?P<attr>\b(?:href|src)\s*=\s*)(?P<q>["']?)\s*"""
    r"""(?:javascript|vbscript|livescript|data:text/html)[^"'\s>]*(?P=q)""",
    re.IGNORECASE,
)


def sanitize_markdown(raw: str) -> SanitizedArtifact:
    """Sanitize a Markdown artifact, preserving Markdown syntax.

    Deliberately regex-based rather than nh3. nh3 is an HTML sanitizer, and
    running it over Markdown *source* HTML-escapes the plain text: `> quote`
    becomes `&gt; quote`, which silently destroys every blockquote, and `a < b`
    becomes `a &lt; b`. Verified empirically before choosing this path.

    Markdown artifacts are stored as Markdown and rendered client-side through
    marked + DOMPurify, so raw HTML inside them is sanitized again at render
    time. This pass exists so the *stored* content is safe on its own, and does
    not depend on every future consumer remembering to sanitize.
    """
    report = SanitizationReport()
    cleaned = raw

    for pattern in (_MD_PAIRED_RE, _MD_UNCLOSED_RE, _MD_VOID_RE):
        def _drop(match: re.Match[str]) -> str:
            tag = (match.group(1) if match.lastindex else "element").lower()
            report.removed_elements.append(tag)
            return ""

        cleaned = pattern.sub(_drop, cleaned)

    def _drop_handler(match: re.Match[str]) -> str:
        report.removed_attributes.append(match.group(0).strip().split("=")[0])
        return ""

    cleaned = _MD_EVENT_RE.sub(_drop_handler, cleaned)

    def _block_link(match: re.Match[str]) -> str:
        report.rewritten_urls.append(match.group(0)[:60])
        return f"{match.group('text')}(blocked:)"

    cleaned = _MD_LINK_SCHEME_RE.sub(_block_link, cleaned)

    def _block_attr(match: re.Match[str]) -> str:
        report.rewritten_urls.append(match.group(0)[:60])
        quote = match.group("q") or '"'
        return f"{match.group('attr')}{quote}blocked:{quote}"

    cleaned = _MD_ATTR_SCHEME_RE.sub(_block_attr, cleaned)

    if report.changed:
        log.info("artifact.sanitized", extra={"kind": "markdown", **report.to_dict()})
    return SanitizedArtifact(content=cleaned, report=report)


def sanitize(raw: str, kind: str) -> SanitizedArtifact:
    """Dispatch by artifact kind."""
    if kind == "html":
        return sanitize_html(raw)
    if kind == "markdown":
        return sanitize_markdown(raw)
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
