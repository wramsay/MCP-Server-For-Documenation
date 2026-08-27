from __future__ import annotations

import configparser
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from docs_mcp_core import DocsMcpError, compact_text


IGNORED_DIRS = {
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

MANIFEST_NAMES = {
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
        except Exception as exc:
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
