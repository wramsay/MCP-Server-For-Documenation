from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from docs_mcp_core import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_FETCH_CHARS,
    MAX_SEARCH_LIMIT,
    SERVER_NAME,
    SERVER_VERSION,
    DocsMcpError,
    coerce_int,
    compact_text,
)


class HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._current_link_href = ""
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "div", "section", "article", "h1", "h2", "h3", "li", "tr"}:
            self._parts.append("\n")
        if tag == "a":
            self._current_link_href = attr.get("href", "")
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._current_link_href:
            text = compact_text(" ".join(self._current_link_text))
            if text:
                self._links.append((text, self._current_link_href))
            self._current_link_href = ""
            self._current_link_text = []
        if tag in {"p", "div", "section", "article", "h1", "h2", "h3", "li", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self._parts.append(data)
        if self._current_link_href:
            self._current_link_text.append(data)

    def text(self) -> str:
        text = html.unescape(" ".join(self._parts))
        lines = [compact_text(line) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    def links(self, base_url: str, limit: int = 30) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for label, href in self._links:
            absolute = urllib.parse.urljoin(base_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append({"label": label[:120], "url": absolute})
            if len(links) >= limit:
                break
        return links


def validate_http_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise DocsMcpError("Only http and https URLs can be fetched")
    if not parsed.netloc:
        raise DocsMcpError("URL must include a host")
    return url


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[bytes, dict[str, str], str]:
    url = validate_http_url(url)
    headers = {
        "User-Agent": (
            "up-to-date-docs-mcp/0.1 "
            "(project documentation assistant; +https://modelcontextprotocol.io)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    }
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
            final_url = response.geturl()
            return body, headers, final_url
    except urllib.error.HTTPError as exc:
        detail = exc.read(800).decode("utf-8", errors="replace")
        raise DocsMcpError(f"HTTP {exc.code} fetching {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        try:
            return curl_fetch(url, headers, "GET", timeout)
        except DocsMcpError:
            pass
        raise DocsMcpError(f"Network error fetching {url}: {exc.reason}") from exc


def fetch_doc(url: str, max_chars: int = 12000) -> dict[str, Any]:
    url = compact_text(url)
    if not url:
        raise DocsMcpError("url is required")
    validate_http_url(url)
    max_chars = max(1000, min(coerce_int(max_chars, "max_chars"), MAX_FETCH_CHARS))
    body, headers, final_url = http_get(url)
    content_type = headers.get("content-type", "")
    is_pdf = "application/pdf" in content_type.lower() or final_url.lower().endswith(".pdf")
    if is_pdf:
        return extract_pdf_text(body, final_url, max_chars)
    text, title, links = extract_html_text(body, final_url)
    return {
        "url": final_url,
        "content_type": content_type or "unknown",
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "links": links,
    }


def extract_html_text(body: bytes, url: str) -> tuple[str, str, list[dict[str, str]]]:
    raw = body.decode("utf-8", errors="replace")
    parser = HtmlTextExtractor()
    parser.feed(raw)
    text = parser.text()
    title = compact_text(parser.title)
    if not text:
        text = compact_text(re.sub(r"<[^>]+>", " ", raw))
    return text, title, parser.links(url)


def extract_pdf_text(body: bytes, url: str, max_chars: int) -> dict[str, Any]:
    text = ""
    extractor = "none"
    try:
        import pypdf  # type: ignore
    except Exception:
        pypdf = None  # type: ignore

    if pypdf is not None:
        extractor = "pypdf"
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(body)
            tmp.flush()
            try:
                pdf = pypdf.PdfReader(tmp.name)
                text = "\n".join((page.extract_text() or "") for page in pdf.pages)
            except Exception as exc:
                text = f"PDF text extraction failed with pypdf: {exc}"

    if not text:
        pdftotext = find_executable("pdftotext")
        if pdftotext:
            extractor = "pdftotext"
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as source:
                source.write(body)
                source.flush()
                result = subprocess.run(
                    [pdftotext, source.name, "-"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                    check=False,
                )
            if result.returncode == 0:
                text = result.stdout
            else:
                text = f"PDF text extraction failed with pdftotext: {result.stderr[:800]}"

    if not text:
        text = (
            "This URL is a PDF. Install pypdf or poppler's pdftotext for full PDF "
            "text extraction, or ask the model to search for an HTML version."
        )
    return {
        "url": url,
        "content_type": "application/pdf",
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "extractor": extractor,
        "bytes": len(body),
    }


def find_executable(name: str) -> str | None:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def context7_search(query: str, limit: int = 5) -> dict[str, Any]:
    if not query.strip():
        raise DocsMcpError("query is required")
    limit = max(1, min(coerce_int(limit, "limit"), MAX_SEARCH_LIMIT))
    api_key = os.environ.get("CONTEXT7_API_KEY", "")
    url = "https://context7.com/api/v1/search?" + urllib.parse.urlencode({"query": query})
    headers = {
        "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        data = json_request("GET", url, headers=headers)
    except DocsMcpError as exc:
        return {
            "error": str(exc),
            "hint": (
                "Context7 may require CONTEXT7_API_KEY. You can still use fetch_doc "
                "when you already have a documentation URL."
            ),
        }
    results = data.get("results") or data.get("libraries") or data
    if isinstance(results, list):
        results = results[:limit]
    return {"query": query, "results": results}


def context7_docs(library_id: str, topic: str = "", tokens: int = 6000) -> dict[str, Any]:
    if not library_id.strip():
        raise DocsMcpError("library_id is required")
    tokens = max(1000, min(coerce_int(tokens, "tokens"), 20000))
    api_key = os.environ.get("CONTEXT7_API_KEY", "")
    path = f"https://context7.com/api/v1{library_id}"
    if not path.endswith("/docs"):
        path = path.rstrip("/") + "/docs"
    query = {"tokens": str(tokens)}
    if topic:
        query["topic"] = topic
    url = path + "?" + urllib.parse.urlencode(query)
    headers = {
        "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        "Accept": "application/json,text/plain;q=0.9,*/*;q=0.5",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        body, response_headers, final_url = http_get_with_headers(url, headers)
    except DocsMcpError as exc:
        return {
            "error": str(exc),
            "hint": (
                "Use context7_search first to get the exact library_id, or set "
                "CONTEXT7_API_KEY if your account requires one."
            ),
        }
    content_type = response_headers.get("content-type", "")
    raw = body.decode("utf-8", errors="replace")
    if "json" in content_type:
        try:
            return {"url": final_url, "content_type": content_type, "data": json.loads(raw)}
        except json.JSONDecodeError:
            pass
    return {"url": final_url, "content_type": content_type, "text": raw[:MAX_FETCH_CHARS]}


def json_request(method: str, url: str, headers: dict[str, str]) -> Any:
    body, _, _ = http_get_with_headers(url, headers, method)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DocsMcpError(f"Expected JSON from {url}: {exc}") from exc


def http_get_with_headers(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, dict[str, str], str]:
    url = validate_http_url(url)
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return (
                response.read(),
                {k.lower(): v for k, v in response.headers.items()},
                response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read(800).decode("utf-8", errors="replace")
        raise DocsMcpError(f"HTTP {exc.code} fetching {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        try:
            return curl_fetch(url, headers, method, timeout)
        except DocsMcpError:
            pass
        raise DocsMcpError(f"Network error fetching {url}: {exc.reason}") from exc


def curl_fetch(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, dict[str, str], str]:
    curl = find_executable("curl")
    if not curl:
        raise DocsMcpError("curl is not available for fallback fetching")
    marker = b"\n__DOCS_MCP_CURL_META__"
    command = [
        curl,
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout),
    ]
    if method and method.upper() != "GET":
        command.extend(["-X", method.upper()])
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.extend(["--write-out", marker.decode("ascii") + "%{json}", url])
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout + 5,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:800]
        raise DocsMcpError(f"curl failed fetching {url}: {detail}")
    if marker not in result.stdout:
        raise DocsMcpError(f"curl response from {url} did not include metadata")
    body, raw_meta = result.stdout.rsplit(marker, 1)
    try:
        meta = json.loads(raw_meta.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DocsMcpError(f"curl metadata parse failed for {url}: {exc}") from exc
    content_type = meta.get("content_type") or ""
    final_url = meta.get("url_effective") or url
    return body, {"content-type": content_type}, final_url
