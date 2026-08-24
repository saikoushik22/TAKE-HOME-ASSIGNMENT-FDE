"""Artifact sanitization — the security boundary.

Generated HTML is untrusted input rendered in a browser. These tests are the
executable form of the guarantees documented in architecture.md section 8, and
they are the tests most worth keeping green: a regression here is a live XSS or
an exfiltration channel, not a cosmetic bug.
"""

from __future__ import annotations

import pytest

from app.security.sanitize import CSP, build_srcdoc, sanitize, sanitize_html


# ------------------------------------------------------- egress must die

# The HTML policy is NOT "remove all script". It is "remove every way an
# artifact can reach the network or escape its frame", and let CSP plus the
# opaque-origin sandbox contain whatever script remains. See architecture.md
# section 8.2 for the permit/block table, and the "permitted but contained"
# group below for the deliberate exceptions.
#
# Each row is a distinct technique, so a newly discovered bypass is added as a
# row rather than as a new test.
EGRESS_VECTORS = [
    pytest.param('<script src="https://evil.test/x.js"></script>', 'evil.test', id='remote-script'),
    pytest.param('<img src=x onerror="alert(1)">', 'onerror', id='event-handler-onerror'),
    pytest.param('<div onclick="steal()">x</div>', 'onclick', id='event-handler-onclick'),
    pytest.param('<body onload="alert(1)">x</body>', 'onload', id='event-handler-onload'),
    pytest.param('<a href="javascript:alert(1)">click</a>', 'javascript:', id='javascript-url'),
    pytest.param('<a href="JaVaScRiPt:alert(1)">click</a>', 'javascript:', id='javascript-url-mixed-case'),
    pytest.param('<a href="vbscript:msgbox(1)">x</a>', 'vbscript:', id='vbscript-url'),
    pytest.param('<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>', 'data:text/html', id='data-html-url'),
    pytest.param('<iframe src="https://evil.test"></iframe>', '<iframe', id='nested-iframe'),
    pytest.param('<object data="evil.swf"></object>', '<object', id='object'),
    pytest.param('<embed src="evil.swf">', '<embed', id='embed'),
    pytest.param('<base href="https://evil.test/">', '<base', id='base-hijack'),
    pytest.param('<link rel="stylesheet" href="https://evil.test/x.css">', '<link', id='remote-stylesheet'),
    pytest.param('<meta http-equiv="refresh" content="0;url=https://evil.test">', 'http-equiv', id='meta-refresh'),
    pytest.param('<div style="background:url(https://evil.test/t.png)">x</div>', 'evil.test', id='css-network-url'),
    pytest.param('<style>@import url(https://evil.test/x.css);</style>', 'evil.test', id='css-import'),
]


@pytest.mark.parametrize('payload,forbidden', EGRESS_VECTORS)
def test_egress_vector_is_neutralized(payload: str, forbidden: str) -> None:
    result = sanitize_html(payload)
    assert forbidden.lower() not in result.content.lower(), (
        f"Sanitizer left {forbidden!r} in output: {result.content!r}"
    )


def test_egress_removals_are_reported_not_silent() -> None:
    """A removal the user cannot see becomes a bug report about a broken artifact."""
    result = sanitize_html('<p>Fine</p><iframe src="https://evil.test"></iframe>')
    assert result.report.changed is True
    assert 'Fine' in result.content


# ------------------------------------------- permitted, but contained

def test_inline_script_is_kept_deliberately() -> None:
    """Inline JS is a FEATURE (charts, interactivity), not an oversight.

    It is safe only because of what surrounds it: the artifact renders inside
    `<iframe sandbox="allow-scripts">` with no `allow-same-origin`, into an
    opaque origin, under a CSP of `default-src 'none'`. Script can therefore
    execute but cannot reach the network, the parent DOM, cookies, or storage.

    If this test ever fails, the containment story changed and
    architecture.md section 8.2 must change with it.
    """
    result = sanitize_html('<div id="c"></div><script>document.title="ok"</script>')
    assert '<script>' in result.content

    # The containment mechanisms this permission depends on must still exist.
    assert "default-src 'none'" in CSP
    assert "frame-ancestors 'none'" in CSP


def test_form_element_survives_but_submission_is_blocked_by_csp() -> None:
    """`form-action 'none'` neutralizes the exfiltration channel.

    Stripping <form> outright would break legitimate rendered mock-ups, so the
    CSP does the security work instead of the parser.
    """
    result = sanitize_html('<form action="https://evil.test"><input name="p"></form>')
    assert "form-action 'none'" in CSP
    assert '<form' in result.content


def test_clean_html_passes_through_unchanged() -> None:
    """Over-aggressive sanitizing would make the feature useless."""
    html = (
        '<h1>Growth review</h1>'
        '<p>Retention is the <strong>compounding</strong> metric.</p>'
        '<ul><li>Activation</li><li>Habit</li></ul>'
        '<table><tr><th>Week</th><td>1</td></tr></table>'
    )
    result = sanitize_html(html)
    assert result.report.changed is False
    for fragment in ('<h1>', '<strong>', '<ul>', '<table>', 'compounding'):
        assert fragment in result.content


def test_inline_styles_survive() -> None:
    """Artifacts are useless without CSS. The iframe, not the stripper, contains them."""
    result = sanitize_html('<style>.card{color:#333;padding:8px}</style><div class="card">Hi</div>')
    assert '.card' in result.content
    assert 'padding' in result.content


def test_data_uri_images_are_allowed() -> None:
    """Self-contained images carry no egress, so they are safe to keep."""
    tiny = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='
    result = sanitize_html(f'<img src="{tiny}" alt="chart">')
    assert 'data:image/png' in result.content


# ------------------------------------------------------------------- CSP


def test_csp_blocks_all_network_egress() -> None:
    """`default-src 'none'` is the anti-exfiltration guarantee."""
    assert "default-src 'none'" in CSP


def test_csp_blocks_form_and_base_hijacking() -> None:
    assert "form-action 'none'" in CSP
    assert "base-uri 'none'" in CSP


def test_srcdoc_carries_the_csp_and_escapes_title() -> None:
    document = build_srcdoc('<p>Body</p>', title='Q3 <Growth> & Retention')
    assert 'Content-Security-Policy' in document
    assert "default-src 'none'" in document
    # An unescaped title would let the title itself break out of the tag.
    assert '<Growth>' not in document
    assert '&lt;Growth&gt;' in document
    assert '&amp;' in document


def test_srcdoc_never_references_a_remote_origin() -> None:
    document = build_srcdoc('<p>x</p>', title='t')
    assert 'http://' not in document
    assert 'https://' not in document


# -------------------------------------------------------------- markdown


def test_markdown_strips_embedded_html_hazards() -> None:
    """Markdown permits raw inline HTML, so it is not inherently safe."""
    result = sanitize('# Title\n\n<script>alert(1)</script>\n\nText.', 'markdown')
    assert 'alert(1)' not in result.content
    assert '# Title' in result.content  # Markdown syntax must be preserved


def test_markdown_neutralizes_javascript_links() -> None:
    result = sanitize('[click me](javascript:alert(1))', 'markdown')
    assert 'javascript:' not in result.content.lower()


def test_markdown_keeps_normal_links() -> None:
    result = sanitize('[Lenny](https://www.lennysnewsletter.com)', 'markdown')
    assert 'https://www.lennysnewsletter.com' in result.content


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        sanitize('<p>x</p>', 'pdf')


# ------------------------------------------------- sandbox attribute rule


def test_viewer_never_pairs_allow_scripts_with_allow_same_origin() -> None:
    """The one mistake that defeats iframe sandboxing entirely.

    Granting `allow-scripts` and `allow-same-origin` together lets framed
    script reach into the parent origin. This asserts against the frontend
    source directly, because the guarantee lives in that string.
    """
    from pathlib import Path

    viewer = (
        Path(__file__).resolve().parents[2]
        / 'frontend' / 'src' / 'components' / 'ArtifactViewer.tsx'
    )
    if not viewer.exists():  # backend-only checkout
        pytest.skip('frontend source not present')

    source = viewer.read_text(encoding='utf-8')
    sandbox_line = next(
        (line for line in source.splitlines() if 'const SANDBOX' in line), ''
    )
    assert 'allow-scripts' in sandbox_line
    assert 'allow-same-origin' not in sandbox_line
