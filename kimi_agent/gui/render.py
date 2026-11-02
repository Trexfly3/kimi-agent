"""Markdown and code rendering for the GUI.

Raw HTML in model output is never passed through (markdown-it runs with ``html=False``), link targets are
validated by markdown-it, and remote images are turned into links so a page cannot load third-party URLs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml, unescapeAll
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

MAX_HIGHLIGHT_CHARS = 120_000
LIGHT_STYLE = "friendly"
DARK_STYLE = "one-dark"
COPY_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/>'
             '<path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>')
_FORMATTER = HtmlFormatter(nowrap=True)


@lru_cache(maxsize=256)
def _lexer(lang: str) -> Any:
    try:
        return get_lexer_by_name(lang, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def highlight_code(code: str, lang: str = "") -> str:
    """Return syntax-highlighted, HTML-escaped code (no wrapper)."""
    lexer = _lexer(lang.lower()) if lang else None
    if lexer is None or len(code) > MAX_HIGHLIGHT_CHARS:
        return escapeHtml(code)
    return highlight(code, lexer, _FORMATTER)


def code_block(code: str, lang: str = "", label: str | None = None) -> str:
    """A code block with a header (language label + copy button), as used in messages and previews."""
    shown = escapeHtml(label if label is not None else (lang or "text"))
    return (f'<div class="code-block"><div class="code-head"><span class="code-lang">{shown}</span>'
            f'<button type="button" class="copy-btn" title="Copy">{COPY_ICON}<span>Copy</span></button></div>'
            f'<pre><code>{highlight_code(code, lang)}</code></pre></div>\n')


def _fence(renderer: Any, tokens: list[Any], idx: int, options: Any, env: Any) -> str:
    token = tokens[idx]
    info = unescapeAll(token.info).strip() if token.info else ""
    lang = info.split(maxsplit=1)[0] if info else ""
    return code_block(token.content, lang)


def _code_block(renderer: Any, tokens: list[Any], idx: int, options: Any, env: Any) -> str:
    return code_block(tokens[idx].content)


def _link_open(renderer: Any, tokens: list[Any], idx: int, options: Any, env: Any) -> str:
    token = tokens[idx]
    token.attrSet("target", "_blank")
    token.attrSet("rel", "noopener noreferrer")
    return renderer.renderToken(tokens, idx, options, env)


def _image(renderer: Any, tokens: list[Any], idx: int, options: Any, env: Any) -> str:
    token = tokens[idx]
    src = str(token.attrGet("src") or "")
    alt = renderer.renderInlineAsText(token.children or [], options, env) or "image"
    if src.startswith("data:image/"):
        return f'<img src="{escapeHtml(src)}" alt="{escapeHtml(alt)}">'
    return (f'<a href="{escapeHtml(src)}" target="_blank" rel="noopener noreferrer" class="image-link">'
            f'{escapeHtml(alt)}</a>')


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False, "breaks": False})
    md.enable(["table", "strikethrough"])
    md.add_render_rule("fence", _fence)
    md.add_render_rule("code_block", _code_block)
    md.add_render_rule("link_open", _link_open)
    md.add_render_rule("image", _image)
    return md


_MD = _build()


def render_markdown(text: str) -> str:
    """Render untrusted Markdown to safe HTML."""
    if not text:
        return ""
    return _MD.render(text)


def _scoped(style: str, scope: str) -> str:
    rules = HtmlFormatter(style=style).get_style_defs(scope).splitlines()
    # Keep only token rules; the unscoped pre/linenos rules and the block background come from app.css.
    return "\n".join(line for line in rules if line.startswith(scope + " ."))


@lru_cache(maxsize=1)
def pygments_css() -> str:
    return (f"/* light: {LIGHT_STYLE} */\n{_scoped(LIGHT_STYLE, '.code-block')}\n"
            f"/* dark: {DARK_STYLE} */\n{_scoped(DARK_STYLE, ':root[data-theme=dark] .code-block')}\n")
