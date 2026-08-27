from __future__ import annotations

import json
from typing import Any

from docs_mcp_core import (
    LEGACY_PROTOCOL_VERSION,
    MAX_FETCH_CHARS,
    MODERN_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    DocsMcpError,
    coerce_int,
    server_meta,
)
from docs_mcp_project import resolve_project_root, scan_project
from docs_mcp_web import context7_docs, context7_search, fetch_doc


SERVER_INSTRUCTIONS = (
    "Use analyze_project first when a question depends on the current "
    "folder. Use fetch_doc when you already have a documentation URL. "
    "Use Context7 tools for software library docs."
)


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
        ],
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
        "supportedProtocolVersions": SUPPORTED_PROTOCOL_VERSIONS,
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocolVersion": MODERN_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": SERVER_INSTRUCTIONS,
        "ttlMs": 300000,
        "cacheScope": "server",
    }


def initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": LEGACY_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": SERVER_INSTRUCTIONS,
    }


def server_discover_result() -> dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocolVersion": MODERN_PROTOCOL_VERSION,
        "supportedProtocolVersions": SUPPORTED_PROTOCOL_VERSIONS,
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": SERVER_INSTRUCTIONS,
    }
