from __future__ import annotations

import html
import re
import sys
from typing import Any


SERVER_NAME = "up-to-date-docs"
SERVER_VERSION = "0.1.0"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = [
    MODERN_PROTOCOL_VERSION,
    "2025-11-25",
    LEGACY_PROTOCOL_VERSION,
]

DEFAULT_TIMEOUT_SECONDS = 20
MAX_SEARCH_LIMIT = 10
MAX_FETCH_CHARS = 50000


class DocsMcpError(Exception):
    """Error that should be returned to the MCP client."""


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
