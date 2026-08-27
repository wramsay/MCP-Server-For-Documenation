# Up-to-Date Docs MCP Server

This workspace contains a small MCP server for inspecting projects and fetching known documentation URLs while you work. It is designed for mixed projects: software repos, electronics folders, firmware, KiCad/PlatformIO/Arduino work, SPICE files, and datasheet-heavy debugging.

The server is intentionally stdlib-only Python so it can run without `npm install`. It uses Python's built-in HTTP client first and falls back to verified `curl -L` fetching when the local Python certificate store is missing or stale.

## What It Provides

- `analyze_project`: scans a project folder for manifests, dependencies, frameworks, firmware/electronics files, and likely docs topics.
- `fetch_doc`: fetches a documentation page or datasheet URL and returns readable text. HTML works out of the box. PDFs are better if `pypdf` or `pdftotext` is installed.
- `context7_search`: optional search against Context7 for software library docs.
- `context7_docs`: optional Context7 docs fetch after you resolve a library ID.

## Quick Smoke Test

```sh
python3 up_to_date_docs_mcp.py --self-test
```

## Generic MCP Config

Use the absolute path to this script:

```json
{
  "mcpServers": {
    "up-to-date-docs": {
      "command": "python3",
      "args": [
        "/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS/up_to_date_docs_mcp.py"
      ],
      "env": {
        "DOCS_MCP_PROJECT_ROOT": "/path/to/your/current/project"
      }
    }
  }
}
```

Keep `up_to_date_docs_mcp.py` in the same folder as the `docs_mcp_*.py` helper files. The entrypoint imports those modules at runtime.

For a truly project-by-project setup, either:

- set the server working directory to the project folder in your MCP client, or
- change `DOCS_MCP_PROJECT_ROOT` when you switch projects, or
- pass a `path` argument when calling `analyze_project`.

## Codex Setup

Codex reads MCP servers from its global config. If the `codex` command is on your `PATH`, register this server with:

```sh
codex mcp add up-to-date-docs \
  --env DOCS_MCP_PROJECT_ROOT="/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS" \
  -- "/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS/.venv/bin/python" \
  "/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS/up_to_date_docs_mcp.py"
```

If your terminal says `codex: command not found`, use the ChatGPT app's bundled Codex CLI:

```sh
/Applications/ChatGPT.app/Contents/Resources/codex mcp add up-to-date-docs \
  --env DOCS_MCP_PROJECT_ROOT="/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS" \
  -- "/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS/.venv/bin/python" \
  "/Users/williamramsay/Documents/MCP SERVER FOR UP TO DATE DOCS/up_to_date_docs_mcp.py"
```

Confirm Codex can see it:

```sh
/Applications/ChatGPT.app/Contents/Resources/codex mcp list
```

Start a new Codex session after adding the server, then run:

```text
/mcp
```

You should see `up-to-date-docs` listed as an enabled MCP server. From there, ask Codex to use `up-to-date-docs` to analyze a project or fetch a documentation URL.

## Optional Context7

Context7 is useful for software libraries because it has docs indexed by library. This server still works without it, but you can add:

```json
{
  "env": {
    "CONTEXT7_API_KEY": "your_key_here"
  }
}
```

Then use `context7_search` first, followed by `context7_docs`.

## Example Prompts

- "Analyze this project and suggest documentation topics for the main framework."
- "Fetch this datasheet URL and summarize the absolute maximum ratings."
- "Use Context7 to get current FastAPI docs for dependency injection."
- "Analyze this firmware folder and suggest documentation topics to look up."

## Notes

- The server fetches live web pages when given URLs, so your MCP client must allow network access for the server process.
- General web search is intentionally not exposed until there is a reliable search backend.
- It supports the newer `server/discover` flow and the older `initialize` flow, which helps across different MCP clients.
- PDF extraction is optional. For electronics work, installing `pypdf` or Poppler's `pdftotext` makes datasheets much more useful.
- Logs go to stderr so stdout stays reserved for MCP protocol messages.
