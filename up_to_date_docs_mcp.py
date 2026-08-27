#!/usr/bin/env python3
"""
Project-aware MCP server for current documentation.

This server intentionally uses only the Python standard library. It speaks
newline-delimited JSON-RPC over stdio, which keeps it easy to run anywhere a
Python 3.11+ interpreter is available.
"""

from __future__ import annotations#chnages how python handles type hints and allow it to handle it better

import argparse #an import allowing to create commands for the terminal

import configparser #brings in Python’s built-in tool for reading .ini configuration files. for microcontroller readings

import html #bring in python tools for reading and handling html text

import json #lets python read and write json data

import os #gived python access to opeerating system features 

import re #lets code pick up on patterns and search,extract,and clean

import subprocess #lets python run other command line programs 

import sys #sys lets the server read from stdin, write to stdout, and log to stderr so it can communicate with the MCP client.

import tempfile #lets python create temo files that are meant to only exist for abrief period of time 

import tomllib #configuration file format for .toml files 

import urllib.error #contains error types used by python's  built iin web fetching tools 

import urllib.parse #built in tools for working with URL's 

import urllib.request #pythons built in tool for making web requests

from html.parser import HTMLParser #built in html parsing class 

from pathlib import Path #makes file and foler paths easier andf safer towoerk with than plain strings 

from typing import Any #helps with establising that a type hint could be anything


SERVER_NAME = "up-to-date-docs"
SERVER_VERSION = "0.1.0"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo" # give extra information about response to MCP client
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = [ #versions of MCP that are supported with this server 
    MODERN_PROTOCOL_VERSION,
    "2025-11-25",
    LEGACY_PROTOCOL_VERSION,
]

DEFAULT_TIMEOUT_SECONDS = 20 
MAX_SEARCH_LIMIT = 10
MAX_FETCH_CHARS = 50000

IGNORED_DIRS = { #These file extensions are what the client should ignore in the current directory
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".next",
    ".nuxt",
    ".cache",
}

MANIFEST_NAMES = { #certain maifests 
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "platformio.ini",
    "arduino-cli.yaml",
    "CMakeLists.txt",
    "idf_component.yml",
}

ELECTRONICS_SUFFIXES = {
    ".kicad_pro",
    ".kicad_sch",
    ".kicad_pcb",
    ".sch",
    ".brd",
    ".asc",
    ".lib",
    ".subckt",
    ".cir",
    ".net",
    ".ino",
}

class DocsMcpError(Exception):
    """Error that should be returned to the MCP client."""


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


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def server_meta() -> dict[str, Any]:
    return {SERVER_INFO_META_KEY: server_info()}


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def coerce_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DocsMcpError(f"{name} must be an integer") from exc


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


def resolve_project_root(path_arg: str | None = None) -> Path:
    raw = (
        path_arg
        or os.environ.get("DOCS_MCP_PROJECT_ROOT")
        or os.environ.get("PROJECT_ROOT")
        or os.getcwd()
    )
    return Path(raw).expanduser().resolve()


def scan_project(path: Path, max_files: int = 250) -> dict[str, Any]:
    if not path.exists():
        raise DocsMcpError(f"Project path does not exist: {path}")
    if path.is_file():
        path = path.parent

    manifest_files: list[Path] = []
    electronics_files: list[Path] = []
    url_mentions: list[str] = []
    package_names: list[str] = []
    languages: set[str] = set()
    frameworks: set[str] = set()
    electronics_topics: set[str] = set()
    scanned = 0

    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
        root_path = Path(root)
        for filename in filenames:
            scanned += 1
            if scanned > max_files:
                break
            file_path = root_path / filename
            suffix = file_path.suffix.lower()
            if filename in MANIFEST_NAMES:
                manifest_files.append(file_path)
            if suffix in ELECTRONICS_SUFFIXES:
                electronics_files.append(file_path)
            if suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".cpp", ".h"}:
                languages.add(language_from_suffix(suffix))
        if scanned > max_files:
            break

    for manifest in manifest_files[:40]:
        try:
            detected = parse_manifest(manifest)
        except Exception as exc:  # A project scan should stay resilient.
            detected = {"warnings": [f"{manifest.name}: {exc}"]}
        package_names.extend(detected.get("packages", []))
        languages.update(detected.get("languages", []))
        frameworks.update(detected.get("frameworks", []))
        electronics_topics.update(detected.get("electronics_topics", []))

    for file_path in electronics_files[:40]:
        electronics_topics.update(electronics_topics_from_filename(file_path.name))
        if file_path.suffix.lower() in {".ino"}:
            electronics_topics.add("Arduino")
        if file_path.suffix.lower() in {".asc", ".cir", ".subckt"}:
            electronics_topics.add("LTspice")
            electronics_topics.add("SPICE simulation")
        if file_path.suffix.lower().startswith(".kicad"):
            electronics_topics.add("KiCad")
            electronics_topics.add("PCB design")

    for candidate in list(manifest_files[:20]) + list(path.glob("README*"))[:5]:
        url_mentions.extend(extract_urls(candidate, limit=20))

    packages = sorted(set(clean_package_name(name) for name in package_names if name))
    topics = sorted(set(frameworks) | set(packages[:20]) | set(electronics_topics))

    return {
        "project_root": str(path),
        "scanned_files": scanned,
        "truncated": scanned > max_files,
        "languages": sorted(x for x in languages if x),
        "frameworks": sorted(frameworks),
        "packages": packages[:80],
        "electronics_topics": sorted(electronics_topics),
        "manifest_files": [str(p.relative_to(path)) for p in manifest_files[:40]],
        "electronics_files": [str(p.relative_to(path)) for p in electronics_files[:40]],
        "url_mentions": sorted(set(url_mentions))[:40],
        "suggested_searches": suggest_searches(topics, languages),
    }


def language_from_suffix(suffix: str) -> str:
    return {
        ".py": "Python",
        ".js": "JavaScript",
        ".jsx": "React/JavaScript",
        ".ts": "TypeScript",
        ".tsx": "React/TypeScript",
        ".go": "Go",
        ".rs": "Rust",
        ".c": "C",
        ".cpp": "C++",
        ".h": "C/C++",
    }.get(suffix, "")


def parse_manifest(path: Path) -> dict[str, Any]:
    name = path.name
    if name == "package.json":
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        packages = []
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            packages.extend((data.get(key) or {}).keys())
        frameworks = detect_frameworks_from_packages(packages)
        return {
            "languages": ["JavaScript/TypeScript"],
            "packages": packages,
            "frameworks": sorted(frameworks),
        }
    if name == "pyproject.toml":
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        packages = []
        project = data.get("project", {})
        packages.extend(parse_python_dependency(x) for x in project.get("dependencies", []) or [])
        optional = project.get("optional-dependencies", {}) or {}
        for values in optional.values():
            packages.extend(parse_python_dependency(x) for x in values)
        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
        packages.extend(k for k in poetry_deps.keys() if k.lower() != "python")
        frameworks = detect_frameworks_from_packages(packages)
        return {"languages": ["Python"], "packages": packages, "frameworks": sorted(frameworks)}
    if name.startswith("requirements") and name.endswith(".txt"):
        packages = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            packages.append(parse_python_dependency(line))
        frameworks = detect_frameworks_from_packages(packages)
        return {"languages": ["Python"], "packages": packages, "frameworks": sorted(frameworks)}
    if name == "Cargo.toml":
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        deps = []
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            deps.extend((data.get(key) or {}).keys())
        return {"languages": ["Rust"], "packages": deps, "frameworks": sorted(detect_frameworks_from_packages(deps))}
    if name == "go.mod":
        packages = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("module"):
                continue
            if line.startswith("require "):
                parts = line.split()
                if len(parts) >= 2:
                    packages.append(parts[1])
        return {"languages": ["Go"], "packages": packages, "frameworks": sorted(detect_frameworks_from_packages(packages))}
    if name == "platformio.ini":
        config = configparser.ConfigParser()
        config.read(path)
        topics = {"PlatformIO"}
        packages = []
        for section in config.sections():
            if config.has_option(section, "platform"):
                topics.add(config.get(section, "platform"))
            if config.has_option(section, "framework"):
                topics.add(config.get(section, "framework"))
            if config.has_option(section, "board"):
                topics.add(config.get(section, "board"))
            if config.has_option(section, "lib_deps"):
                packages.extend(
                    compact_text(x)
                    for x in config.get(section, "lib_deps").splitlines()
                    if compact_text(x)
                )
        return {
            "languages": ["C/C++"],
            "packages": packages,
            "electronics_topics": sorted(topics),
            "frameworks": ["PlatformIO"],
        }
    if name == "CMakeLists.txt":
        text = path.read_text(encoding="utf-8", errors="replace")[:4000].lower()
        topics = []
        if "idf_component" in text or "esp_idf" in text:
            topics.append("ESP-IDF")
        return {"languages": ["C/C++"], "packages": [], "electronics_topics": topics}
    if name == "idf_component.yml":
        return {"languages": ["C/C++"], "electronics_topics": ["ESP-IDF"], "packages": []}
    return {"packages": [], "languages": [], "frameworks": []}


def parse_python_dependency(value: str) -> str:
    value = value.split(";", 1)[0].strip()
    return re.split(r"[<>=~!\[]", value, maxsplit=1)[0].strip()


def clean_package_name(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    value = value.split("@", 1)[0] if value.startswith("@") and value.count("/") == 0 else value
    return value


def detect_frameworks_from_packages(packages: list[str]) -> set[str]:
    lower = {p.lower(): p for p in packages}
    frameworks: set[str] = set()
    checks = {
        "react": "React",
        "next": "Next.js",
        "vue": "Vue",
        "nuxt": "Nuxt",
        "svelte": "Svelte",
        "vite": "Vite",
        "tailwindcss": "Tailwind CSS",
        "express": "Express",
        "fastify": "Fastify",
        "django": "Django",
        "flask": "Flask",
        "fastapi": "FastAPI",
        "pydantic": "Pydantic",
        "pytest": "pytest",
        "tokio": "Tokio",
        "axum": "Axum",
        "actix-web": "Actix Web",
        "gin-gonic/gin": "Gin",
        "arduino": "Arduino",
        "platformio": "PlatformIO",
        "kicad": "KiCad",
    }
    for key, label in checks.items():
        if key in lower or any(key in p for p in lower):
            frameworks.add(label)
    return frameworks


def electronics_topics_from_filename(name: str) -> set[str]:
    lowered = name.lower()
    topics: set[str] = set()
    common = {
        "esp32": "ESP32",
        "esp8266": "ESP8266",
        "stm32": "STM32",
        "rp2040": "RP2040",
        "arduino": "Arduino",
        "opamp": "op amp",
        "buck": "buck converter",
        "boost": "boost converter",
        "mosfet": "MOSFET",
        "bms": "battery management",
        "usb": "USB",
        "uart": "UART",
        "i2c": "I2C",
        "spi": "SPI",
        "can": "CAN bus",
    }
    for needle, label in common.items():
        if needle in lowered:
            topics.add(label)
    return topics


def extract_urls(path: Path, limit: int = 20) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:30000]
    except Exception:
        return []
    urls = re.findall(r"https?://[^\s)>\]\"']+", text)
    return urls[:limit]


def suggest_searches(topics: list[str], languages: set[str]) -> list[str]:
    suggestions: list[str] = []
    for topic in topics[:12]:
        if topic:
            if topic in {"KiCad", "PlatformIO", "Arduino", "ESP-IDF"}:
                suggestions.append(f"{topic} official documentation")
            elif topic.upper() == topic and any(ch.isdigit() for ch in topic):
                suggestions.append(f"{topic} datasheet reference manual")
            else:
                suggestions.append(f"{topic} official docs current")
    for language in sorted(languages):
        suggestions.append(f"{language} official docs")
    return dedupe(suggestions)[:15]


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


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


def call_tool(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    args = args or {}
    if name == "analyze_project":
        return scan_project(
            resolve_project_root(args.get("path")),
            max_files=coerce_int(args.get("max_files", 250), "max_files"),
        )
    if name == "fetch_doc":
        return fetch_doc(
            str(args.get("url", "")),
            max_chars=coerce_int(args.get("max_chars", 12000), "max_chars"),
        )
    if name == "context7_search":
        return context7_search(str(args.get("query", "")), coerce_int(args.get("limit", 5), "limit"))
    if name == "context7_docs":
        return context7_docs(
            str(args.get("library_id", "")),
            str(args.get("topic", "")),
            coerce_int(args.get("tokens", 6000), "tokens"),
        )
    raise DocsMcpError(f"Unknown tool: {name}")


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "analyze_project",
            "description": (
                "Scan the current project folder and detect software stacks, package "
                "manifests, electronics artifacts, and likely documentation topics."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project folder to scan. Defaults to DOCS_MCP_PROJECT_ROOT or the server cwd.",
                    },
                    "max_files": {
                        "type": "integer",
                        "description": "Maximum files to inspect while scanning.",
                        "default": 250,
                    },
                },
            },
        },
        {
            "name": "fetch_doc",
            "description": (
                "Fetch a URL and return readable text plus useful links. HTML works "
                "with no dependencies; PDFs use optional pypdf or pdftotext if present."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "Documentation, article, datasheet, or PDF URL."},
                    "max_chars": {"type": "integer", "default": 12000, "minimum": 1000, "maximum": MAX_FETCH_CHARS},
                },
            },
        },
        {
            "name": "context7_search",
            "description": (
                "Search Context7 for software library documentation IDs. Optional: set "
                "CONTEXT7_API_KEY for accounts/rate limits that require it."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Library or framework name, such as react or fastapi."},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
                },
            },
        },
        {
            "name": "context7_docs",
            "description": (
                "Fetch current Context7 docs for a resolved library ID. Use context7_search first."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["library_id"],
                "properties": {
                    "library_id": {
                        "type": "string",
                        "description": "Context7 library ID, often shaped like /org/project.",
                    },
                    "topic": {"type": "string", "description": "Optional docs topic to narrow the result."},
                    "tokens": {"type": "integer", "default": 6000, "minimum": 1000, "maximum": 20000},
                },
            },
        },
    ]


def success_response(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def error_response(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def text_content(payload: Any) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "_meta": server_meta(),
        "structuredContent": payload,
        "isError": False,
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def tool_call_error(message: str) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "_meta": server_meta(),
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


def tool_list_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "_meta": server_meta(),
        "tools": tools(),
        "ttlMs": 300000,
        "cacheScope": "server",
    }


def discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "_meta": server_meta(),
        "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
        # Compatibility hints for clients that implemented the release-candidate shape.
        "supportedProtocolVersions": SUPPORTED_PROTOCOL_VERSIONS,
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocolVersion": MODERN_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": (
            "Use analyze_project first when a question depends on the current "
            "folder. Use fetch_doc when you already have a documentation URL. "
            "Use Context7 tools for software library docs."
        ),
        "ttlMs": 300000,
        "cacheScope": "server",
    }


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method is None:
        return error_response(message_id, -32600, "Invalid Request")

    # Notifications have no id and should not receive a response.
    if message_id is None and method.startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            return success_response(
                message_id,
                {
                    "protocolVersion": LEGACY_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Use analyze_project first when a question depends on the current "
                        "folder. Use fetch_doc when you already have a documentation URL. "
                        "Use Context7 tools for software library docs."
                    ),
                },
            )
        if method == "server/discover":
            return success_response(
                message_id,
                {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "protocolVersion": MODERN_PROTOCOL_VERSION,
                    "supportedProtocolVersions": SUPPORTED_PROTOCOL_VERSIONS,
                    "capabilities": {"tools": {"listChanged": False}},
                    "instructions": (
                        "Use analyze_project first when a question depends on the current "
                        "folder. Use fetch_doc when you already have a documentation URL. "
                        "Use Context7 tools for software library docs."
                    ),
                },
            )
        if method == "ping":
            return success_response(message_id, {})
        if method == "tools/list":
            return success_response(message_id, tool_list_result())
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                result = call_tool(tool_name, arguments)
                return success_response(message_id, text_content(result))
            except DocsMcpError as exc:
                return success_response(message_id, tool_call_error(str(exc)))
        return error_response(message_id, -32601, f"Method not found: {method}")
    except DocsMcpError as exc:
        return error_response(message_id, -32000, str(exc))
    except Exception as exc:
        log(f"Unhandled error in {method}: {exc}")
        return error_response(message_id, -32603, f"Internal error: {exc}")


def write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve() -> None:
    log(f"{SERVER_NAME} {SERVER_VERSION} ready; project root {resolve_project_root()}")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            write_message(error_response(None, -32700, f"Parse error: {exc}"))
            continue
        response = handle_request(message)
        if response is not None:
            write_message(response)


def main() -> int:
    parser = argparse.ArgumentParser(description="Project-aware MCP docs server")
    parser.add_argument("--self-test", action="store_true", help="Run a local protocol smoke test")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    serve()
    return 0


def self_test() -> int:
    initialize = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    listing = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    project = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "analyze_project", "arguments": {"path": os.getcwd(), "max_files": 20}},
        }
    )
    checks = [
        initialize and "result" in initialize,
        listing and len(listing.get("result", {}).get("tools", [])) >= 3,
        project and "result" in project,
    ]
    if all(checks):
        print("self-test ok")
        return 0
    print("self-test failed", file=sys.stderr)
    print(json.dumps({"initialize": initialize, "listing": listing, "project": project}, indent=2), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
