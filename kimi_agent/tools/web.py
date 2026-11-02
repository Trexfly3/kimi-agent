"""Web tools: fetch pages (HTML -> Markdown) and search the web."""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from kimi_agent.media import pdf_to_text, prepare_image
from kimi_agent.tools.base import Tool, ToolContext, ToolError, ToolResult, tool

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAX_DOWNLOAD = 10 * 1024 * 1024


class HTMLToMarkdown(HTMLParser):
    """Small dependency-free HTML to Markdown converter tuned for feeding pages to an LLM."""

    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "nav", "footer", "select",
            "button", "head", "object"}  # only elements with end tags: a void element here would hide the rest
    BLOCK = {"p", "div", "section", "article", "main", "header", "aside", "figure", "figcaption", "address",
             "details", "summary", "dl", "dt", "dd", "table", "form", "fieldset", "center"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._pre = 0
        self._lists: list[list[Any]] = []
        self._links: list[tuple[int, str]] = []

    # -- helpers
    def _newline(self) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def _block(self) -> None:
        self._newline()
        self.out.append("\n")

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        attr = {k: (v or "") for k, v in attrs}
        if tag in self.HEADINGS:
            self._block()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self._block()
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self._block()
            self.out.append("---")
            self._block()
        elif tag in ("ul", "ol"):
            self._newline()
            self._lists.append([tag, 0])
        elif tag == "li":
            self._newline()
            indent = "  " * max(0, len(self._lists) - 1)
            if self._lists and self._lists[-1][0] == "ol":
                self._lists[-1][1] += 1
                self.out.append(f"{indent}{self._lists[-1][1]}. ")
            else:
                self.out.append(f"{indent}- ")
        elif tag == "pre":
            self._block()
            self.out.append("```\n")
            self._pre += 1
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "a":
            self._links.append((len(self.out), attr.get("href", "")))
        elif tag == "img":
            alt = attr.get("alt", "").strip()
            if alt:
                src = attr.get("src", "")
                self.out.append(f"![{alt}]({urljoin(self.base_url, src)})" if src else alt)
        elif tag == "tr":
            self._newline()
            self.out.append("| ")
        elif tag == "blockquote":
            self._block()
            self.out.append("> ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in self.HEADINGS or tag in self.BLOCK or tag in ("p", "blockquote"):
            self._block()
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._newline()
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._newline()
            self.out.append("```")
            self._block()
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "a" and self._links:
            start, href = self._links.pop()
            text = re.sub(r"\s+", " ", "".join(self.out[start:])).strip()
            del self.out[start:]
            if text and href and not href.startswith(("javascript:", "#", "mailto:")):
                self.out.append(f"[{text}]({urljoin(self.base_url, href)})")
            elif text:
                self.out.append(text)
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "tr":
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            if self.out and not self.out[-1].endswith((" ", "\n")):
                self.out.append(" ")
            return
        if not self.out or self.out[-1].endswith("\n"):
            text = text.lstrip()
        self.out.append(text)

    def markdown(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """Return (title, markdown)."""
    parser = HTMLToMarkdown(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return " ".join(parser.title.split()), parser.markdown()


def _client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _download(url: str) -> tuple[int, str, str, bytes, str | None, bool]:
    try:
        with _client() as client, client.stream("GET", url) as resp:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    truncated = True
                    break
            return (resp.status_code, str(resp.url), resp.headers.get("content-type", ""), b"".join(chunks),
                    resp.charset_encoding, truncated)
    except httpx.HTTPError as exc:
        raise ToolError(f"Failed to fetch {url}: {type(exc).__name__}: {exc}") from exc


@tool(category="network", summarize=lambda a: str(a.get("url", "")))
def web_fetch(ctx: ToolContext, url: str, max_chars: int = 20000, start_index: int = 0, raw: bool = False) -> ToolResult:
    """Fetch a URL. HTML is converted to Markdown, JSON is pretty-printed, PDFs are converted to text and
    images are shown to you when vision is enabled.

    Long pages are paginated: call again with start_index to continue reading.

    Args:
        url: The http(s) URL to fetch.
        max_chars: Maximum number of characters to return.
        start_index: Character offset to start reading from.
        raw: Return the raw HTML instead of Markdown.
    """
    if "://" not in url:
        url = "https://" + url
    if urlparse(url).scheme not in ("http", "https"):
        raise ToolError("Only http:// and https:// URLs are supported.")
    status, final_url, content_type, body, charset, truncated = _download(url)
    mime = content_type.split(";")[0].strip().lower()
    header = f"URL: {final_url}\nStatus: {status}\n"

    if mime.startswith("image/"):
        if not ctx.vision:
            return ToolResult(header + f"(image {mime}, {len(body):,} bytes; vision is disabled)")
        image, desc = prepare_image(body, mime)
        return ToolResult(header + f"Image ({desc}) attached.", images=[image])

    if mime == "application/pdf" or final_url.lower().endswith(".pdf"):
        text = pdf_to_text(body)
    else:
        decoded = body.decode(charset or "utf-8", errors="replace")
        if ("html" in mime or (not mime and "<html" in decoded[:1000].lower())) and not raw:
            title, text = html_to_markdown(decoded, final_url)
            if title:
                header += f"Title: {title}\n"
        elif "json" in mime:
            try:
                text = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)
            except ValueError:
                text = decoded
        elif mime.startswith("text/") or "xml" in mime or "javascript" in mime or not mime or raw:
            text = decoded
        else:
            return ToolResult(
                header + f"Binary content ({mime}, {len(body):,} bytes) not shown. "
                "Download it with run_command (curl -L -o FILE URL) if needed."
            )

    if truncated:
        header += f"[Download truncated at {MAX_DOWNLOAD // (1024 * 1024)} MB]\n"
    total = len(text)
    start = max(0, int(start_index))
    if start >= total and total:
        raise ToolError(f"start_index {start} is past the end of the content ({total} characters).")
    end = min(total, start + max(1000, int(max_chars)))
    chunk = text[start:end]
    footer = ""
    if start > 0 or end < total:
        footer = f"\n\n[Showing characters {start}-{end} of {total}."
        footer += f" Call again with start_index={end} to read more.]" if end < total else "]"
    return ToolResult(header + "\n" + (chunk or "(empty)") + footer, is_error=status >= 400)


# --------------------------------------------------------------------------- search

class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        classes = attr.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            href = attr.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            elif href.startswith("//"):
                href = "https:" + href
            self.results.append({"title": "", "url": href, "snippet": ""})
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "td", "div"):
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            self.results[-1][self._field] += data


def _search_duckduckgo(query: str, count: int) -> list[dict[str, str]]:
    with _client() as client:
        resp = client.post("https://html.duckduckgo.com/html/", data={"q": query})
        resp.raise_for_status()
    parser = _DDGParser()
    parser.feed(resp.text)
    results = [r for r in parser.results if r["url"].startswith("http") and "duckduckgo.com/y.js" not in r["url"]]
    if not results and "anomaly" in resp.text.lower():
        raise ToolError("DuckDuckGo rate-limited this request. Configure a search API key "
                        "(TAVILY_API_KEY, BRAVE_API_KEY) or SEARXNG_URL.")
    return results[:count]


def _search_tavily(query: str, count: int, key: str) -> list[dict[str, str]]:
    with _client() as client:
        resp = client.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": count},
            headers={"Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in resp.json().get("results", [])]


def _search_brave(query: str, count: int, key: str) -> list[dict[str, str]]:
    with _client() as client:
        resp = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in resp.json().get("web", {}).get("results", [])]


def _search_searxng(query: str, count: int, base: str) -> list[dict[str, str]]:
    with _client() as client:
        resp = client.get(base.rstrip("/") + "/search", params={"q": query, "format": "json"})
        resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in resp.json().get("results", [])][:count]


def search_backend(settings: Any) -> str:
    backend = getattr(settings, "search_backend", "auto")
    if backend != "auto":
        return backend
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY"):
        return "brave"
    if os.environ.get("SEARXNG_URL"):
        return "searxng"
    return "duckduckgo"


@tool(category="network", summarize=lambda a: str(a.get("query", "")))
def web_search(ctx: ToolContext, query: str, max_results: int = 8) -> str:
    """Search the web. Returns titles, URLs and snippets; use web_fetch to read a result in full.

    Args:
        query: The search query.
        max_results: Number of results to return (1-20).
    """
    count = max(1, min(20, int(max_results)))
    backend = search_backend(ctx.settings)
    try:
        if backend == "tavily":
            results = _search_tavily(query, count, os.environ.get("TAVILY_API_KEY", ""))
        elif backend == "brave":
            key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY", "")
            results = _search_brave(query, count, key)
        elif backend == "searxng":
            results = _search_searxng(query, count, os.environ.get("SEARXNG_URL", "http://localhost:8888"))
        elif backend == "duckduckgo":
            results = _search_duckduckgo(query, count)
        else:
            raise ToolError(f"Unknown search backend {backend!r}.")
    except httpx.HTTPError as exc:
        raise ToolError(f"Search via {backend} failed: {type(exc).__name__}: {exc}") from exc
    if not results:
        return f"No results for {query!r} ({backend})."
    lines = []
    for i, r in enumerate(results[:count], 1):
        snippet = " ".join(r.get("snippet", "").split())
        lines.append(f"{i}. {' '.join(r.get('title', '').split()) or '(untitled)'}\n   {r.get('url', '')}"
                     + (f"\n   {snippet}" if snippet else ""))
    return "\n".join(lines)


def kimi_builtin_search_tool() -> Tool:
    """Moonshot's server-side `$web_search` builtin: the client echoes the arguments back unchanged."""
    return Tool(
        name="$web_search",
        description="Kimi built-in web search (executed by the Kimi API).",
        parameters={"type": "object", "properties": {}},
        func=lambda _ctx, /, **kwargs: json.dumps(kwargs, ensure_ascii=False),
        category="network",
        strict_args=False,
        summarize=lambda a: str(a.get("search_result", {}).get("search_id", "")) if isinstance(
            a.get("search_result"), dict) else "kimi search",
        builtin={"type": "builtin_function", "function": {"name": "$web_search"}},
    )


WEB_TOOLS: list[Tool] = [web_fetch, web_search]
