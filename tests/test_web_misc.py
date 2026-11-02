from __future__ import annotations

import json
import struct
import subprocess
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from kimi_agent import prompts
from kimi_agent.attachments import expand_mentions
from kimi_agent.tools import files, web
from kimi_agent.tools.base import ToolError
from kimi_agent.tools.web import html_to_markdown, kimi_builtin_search_tool, search_backend, web_fetch, web_search


def tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def tiny_pdf(text: str = "Hello PDF") -> bytes:
    stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


PAGE = """<html><head><title> Test
   Page </title><style>.x { color: red }</style></head><body>
<nav>menu</nav><h1>Hello</h1><p>Some <b>bold</b> text and a <a href="/docs">link</a>.</p>
<ul><li>one</li><li>two<ol><li>nested</li></ol></li></ul>
<pre><code>x = 1
  y = 2</code></pre><p>inline <code>code</code></p>
<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
<img src="pic.png" alt="A picture"><embed src="x.swf"><p>after embed</p>
<script>alert(1)</script><footer>foot</footer></body></html>"""

ROUTES = {
    "/page": (200, "text/html; charset=utf-8", PAGE.encode()),
    "/data.json": (200, "application/json", b'{"a": 1, "b": [1, 2]}'),
    "/long.txt": (200, "text/plain", b"0123456789" * 3000),
    "/missing": (404, "text/plain", b"not found here"),
    "/bin": (200, "application/octet-stream", b"\x00\x01\x02"),
    "/img.png": (200, "image/png", tiny_png()),
    "/doc.pdf": (200, "application/pdf", tiny_pdf()),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        status, mime, body = ROUTES.get(self.path, (404, "text/plain", b"no route"))
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# --------------------------------------------------------------------------- HTML conversion

def test_html_to_markdown():
    title, md = html_to_markdown(PAGE, "https://example.com/dir/page")
    assert title == "Test Page"
    assert "# Hello" in md and "Some bold text and a [link](https://example.com/docs)." in md
    assert "- one\n- two\n  1. nested" in md
    assert "```\nx = 1\n  y = 2\n```" in md and "inline `code`" in md
    assert "| A | B |" in md and "| 1 | 2 |" in md
    assert "![A picture](https://example.com/dir/pic.png)" in md
    assert "after embed" in md
    for hidden in ("menu", "alert", "foot", "color: red"):
        assert hidden not in md


def test_html_to_markdown_survives_garbage():
    title, md = html_to_markdown("<p>ok<div><<<>>></span></b>&bogus;", "")
    assert "ok" in md and title == ""


# --------------------------------------------------------------------------- web_fetch

def test_fetch_html_and_raw(ctx, site):
    result = web_fetch.run(ctx, {"url": f"{site}/redirect"})
    assert f"URL: {site}/page" in result.output and "Title: Test Page" in result.output
    assert f"[link]({site}/docs)" in result.output and not result.is_error
    raw = web_fetch.run(ctx, {"url": f"{site}/page", "raw": True})
    assert "<h1>Hello</h1>" in raw.output


def test_fetch_json_and_pagination(ctx, site):
    assert '"b": [\n    1,' in web_fetch.run(ctx, {"url": f"{site}/data.json"}).output
    first = web_fetch.run(ctx, {"url": f"{site}/long.txt", "max_chars": 10_000}).output
    assert "[Showing characters 0-10000 of 30000. Call again with start_index=10000" in first
    last = web_fetch.run(ctx, {"url": f"{site}/long.txt", "max_chars": 10_000, "start_index": 25_000}).output
    assert last.endswith("[Showing characters 25000-30000 of 30000.]")
    with pytest.raises(ToolError, match="past the end"):
        web_fetch.run(ctx, {"url": f"{site}/long.txt", "start_index": 40_000})


def test_fetch_errors_and_binary(ctx, site):
    missing = web_fetch.run(ctx, {"url": f"{site}/missing"})
    assert missing.is_error and "Status: 404" in missing.output and "not found here" in missing.output
    assert "Binary content (application/octet-stream" in web_fetch.run(ctx, {"url": f"{site}/bin"}).output
    with pytest.raises(ToolError, match="Only http"):
        web_fetch.run(ctx, {"url": "ftp://example.com/file"})
    with pytest.raises(ToolError, match="Failed to fetch"):
        web_fetch.run(ctx, {"url": "http://127.0.0.1:9/"})


def test_fetch_images_and_pdf(ctx, site):
    assert "vision is disabled" in web_fetch.run(ctx, {"url": f"{site}/img.png"}).output
    ctx.settings.vision = True
    result = web_fetch.run(ctx, {"url": f"{site}/img.png"})
    assert len(result.images) == 1 and result.images[0].mime == "image/png"
    assert "Hello PDF" in web_fetch.run(ctx, {"url": f"{site}/doc.pdf"}).output


# --------------------------------------------------------------------------- web_search

DDG_HTML = """<div class="result"><h2 class="result__title">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Example   A</a></h2>
<a class="result__snippet" href="#">Snippet <b>A</b> text</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Sponsored</a></div>
<div class="result"><a class="result__a" href="https://example.org/b">Example B</a></div>"""


def use_transport(monkeypatch, handler):
    monkeypatch.setattr(web, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_search_backend_selection(settings, monkeypatch):
    assert search_backend(settings) == "duckduckgo"
    monkeypatch.setenv("SEARXNG_URL", "http://searx")
    assert search_backend(settings) == "searxng"
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    assert search_backend(settings) == "brave"
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert search_backend(settings) == "tavily"
    settings.search_backend = "duckduckgo"
    assert search_backend(settings) == "duckduckgo"


def test_duckduckgo(ctx, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=DDG_HTML)
    use_transport(monkeypatch, handler)
    out = web_search.run(ctx, {"query": "example", "max_results": 5}).output
    assert out == ("1. Example A\n   https://example.com/a\n   Snippet A text\n"
                   "2. Example B\n   https://example.org/b")
    assert seen[0].method == "POST" and b"q=example" in seen[0].content


def test_duckduckgo_rate_limit_and_errors(ctx, monkeypatch):
    use_transport(monkeypatch, lambda r: httpx.Response(200, text="<p>anomaly detected</p>"))
    with pytest.raises(ToolError, match="rate-limited"):
        web_search.run(ctx, {"query": "x"})
    use_transport(monkeypatch, lambda r: httpx.Response(200, text="<p>nothing</p>"))
    assert web_search.run(ctx, {"query": "x"}).output == "No results for 'x' (duckduckgo)."
    use_transport(monkeypatch, lambda r: httpx.Response(503, text="down"))
    with pytest.raises(ToolError, match="Search via duckduckgo failed"):
        web_search.run(ctx, {"query": "x"})
    ctx.settings.search_backend = "bing"
    with pytest.raises(ToolError, match="Unknown search backend"):
        web_search.run(ctx, {"query": "x"})


def test_api_backends(ctx, monkeypatch):
    def handler(request):
        if request.url.host == "api.tavily.com":
            body = json.loads(request.content)
            assert body["api_key"] == "tk" and body["max_results"] == 2
            return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t", "content": "tc"}]})
        if request.url.host == "api.search.brave.com":
            assert request.headers["x-subscription-token"] == "bk"
            return httpx.Response(200, json={"web": {"results": [{"title": "B", "url": "https://b",
                                                                  "description": "bd"}]}})
        assert request.url.path == "/search" and request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [{"title": "S", "url": "https://s", "content": "sc"}] * 5})
    use_transport(monkeypatch, handler)
    monkeypatch.setenv("SEARXNG_URL", "http://searx.local/")
    assert web_search.run(ctx, {"query": "q", "max_results": 2}).output.count("https://s") == 2
    monkeypatch.setenv("BRAVE_API_KEY", "bk")
    assert web_search.run(ctx, {"query": "q"}).output == "1. B\n   https://b\n   bd"
    monkeypatch.setenv("TAVILY_API_KEY", "tk")
    assert web_search.run(ctx, {"query": "q", "max_results": 2}).output == "1. T\n   https://t\n   tc"


def test_kimi_builtin_search_echoes_arguments(ctx):
    tool = kimi_builtin_search_tool()
    args = {"search_result": {"search_id": "abc"}, "usage": {"total_tokens": 10}}
    assert json.loads(tool.run(ctx, args).output) == args
    assert tool.builtin == {"type": "builtin_function", "function": {"name": "$web_search"}}


# --------------------------------------------------------------------------- @mentions

def test_expand_mentions(ctx, workdir):
    (workdir / "notes.txt").write_text("hello notes")
    (workdir / "my file.txt").write_text("spaced")
    (workdir / "sub").mkdir()
    (workdir / "sub" / "a.py").write_text("")
    (workdir / "sub" / "node_modules").mkdir()
    (workdir / "image.png").write_bytes(tiny_png())
    (workdir / "bin.dat").write_bytes(b"\x00\x01\x02" * 10)
    (workdir / "doc.pdf").write_bytes(tiny_pdf("Mention PDF"))
    text = ('Look at @notes.txt, @sub and @"my file.txt"; also @missing.txt, @image.png, @bin.dat. '
            'Mail me@example.com about @notes.txt and @doc.pdf')
    content, notes = expand_mentions(text, ctx, vision=False)
    assert isinstance(content, str) and content.startswith(text)
    assert '<file path="notes.txt">\nhello notes\n</file>' in content and content.count("hello notes") == 1
    assert '<directory path="sub">\na.py\n</directory>' in content
    assert '<file path="my file.txt">\nspaced\n</file>' in content and "Mention PDF" in content
    assert notes == ["attached notes.txt", "attached directory listing sub", "attached my file.txt",
                     "skipped image image.png: vision is off for this model", "skipped binary file bin.dat",
                     "attached doc.pdf"]
    parts, notes = expand_mentions("see @image.png", ctx, vision=True)
    assert parts[0] == {"type": "text", "text": "see @image.png"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert expand_mentions("nothing here @nope", ctx, vision=True) == ("nothing here @nope", [])


def test_large_attachments_are_truncated(ctx, workdir):
    (workdir / "big.txt").write_text("x" * 200_000)
    content, notes = expand_mentions("@big.txt", ctx, vision=False)
    assert notes == ["attached big.txt (truncated)"] and "use read_file for the rest" in content
    assert len(content) < 110_000


# --------------------------------------------------------------------------- prompts

def test_project_instructions_order(tmp_path, settings):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("global rule")
    (repo / "AGENTS.md").write_text("root rule")
    (repo / "pkg" / "KIMI.md").write_text("pkg rule")
    (repo / "pkg" / "AGENTS.md").write_text("   ")
    text = prompts.project_instructions(repo / "pkg", data_dir)
    assert text.index("global rule") < text.index("root rule") < text.index("pkg rule")
    assert text.count("<instructions") == 3
    assert prompts.project_instructions(tmp_path, tmp_path / "nothing") == ""


def test_system_prompt_sections(settings, workdir):
    (workdir / "AGENTS.md").write_text("Use tabs.")
    full = prompts.build_system_prompt(settings, workdir, ["remember", "delegate_task", "screenshot"],
                                       memory_text="- prefers pytest", extra="Extra bit")
    for piece in ("# Computer use", "# Memory (facts saved", "prefers pytest", "# Sub-agents", "Use tabs.",
                  "Extra bit", f"Working directory: {workdir}", "fake-model", "Graphical desktop control"):
        assert piece in full
    sub = prompts.build_system_prompt(settings, workdir, ["remember", "delegate_task"], subagent=True)
    assert "# Sub-agent mode" in sub and "# Sub-agents" not in sub and "# Computer use" not in sub


# --------------------------------------------------------------------------- search_files without ripgrep

def test_search_files_python_fallback(ctx, workdir, monkeypatch):
    monkeypatch.setattr(files.shutil, "which", lambda name: None)
    (workdir / "a.py").write_text("def foo():\n    return 1\n")
    (workdir / "b.txt").write_text("foo bar\nFOO baz\n")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "dep.py").write_text("def foo(): pass\n")
    (workdir / "blob.bin").write_bytes(b"foo\x00\x00")
    run = lambda **kw: files.search_files.run(ctx, kw).output
    assert run(pattern="foo") == "a.py:1:def foo():\nb.txt:1:foo bar"
    assert run(pattern="foo", glob="*.py", context_lines=1) == "a.py:1:def foo():\na.py-2-    return 1"
    assert run(pattern="foo", ignore_case=True, path="b.txt") == "b.txt:1:foo bar\nb.txt:2:FOO baz"
    assert run(pattern="foo", path="node_modules") == "node_modules/dep.py:1:def foo(): pass"
    assert run(pattern="zzz").startswith("No matches")
    assert "1 more lines not shown" in run(pattern="foo", max_results=1)
    with pytest.raises(ToolError, match="Invalid regular expression"):
        run(pattern="(")
