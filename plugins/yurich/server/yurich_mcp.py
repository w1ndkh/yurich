"""Dependency-free MCP server for YURICH."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from filesystem_ops import YurichError, choose_folder, open_in_notepad, read_file, save_file, validate_root
from git_ops import git_commit, git_push, git_status
from search_ops import search_files, search_project
from state_ops import load_state, save_state
from terminal_ops import list_package_scripts, poll_command, start_command, stop_command

VERSION = "0.1.0"
UI_URI = "ui://yurich/main-v14.html"
UI_URIS = (
    UI_URI,
    "ui://yurich/main-v13.html",
    "ui://yurich/main-v12.html",
    "ui://yurich/main-v11.html",
    "ui://yurich/main-v10.html",
    "ui://yurich/main-v9.html",
    "ui://yurich/main-v8.html",
    "ui://yurich/main-v7.html",
    "ui://yurich/main-v6.html",
    "ui://yurich/main-v5.html",
    "ui://yurich/main-v4.html",
    "ui://yurich/main-v3.html",
    "ui://yurich/main-v2.html",
    "ui://yurich/main-v1.html",
)
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def send(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def answer(request_id: Any, value: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def fail(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object", "properties": properties, "additionalProperties": False
    }
    if required:
        value["required"] = required
    return value


ROOT = {"type": "string", "description": "Absolute local folder path."}
FILE = {"type": "string", "description": "File path relative to the selected folder."}
VERSION_SCHEMA = schema({
    "size": {"type": "integer"},
    "mtimeNs": {"type": "integer"},
    "sha256": {"type": "string"},
}, ["size", "mtimeNs", "sha256"])

TOOLS = [
    {
        "name": "open_yurich",
        "title": "Open YURICH",
        "description": "Open the YURICH local folder search and quick-edit interface.",
        "inputSchema": schema({"root": ROOT}),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {
            "ui": {"resourceUri": UI_URI},
            "openai/outputTemplate": UI_URI,
            "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": "Opening YURICH...",
            "openai/toolInvocation/invoked": "YURICH is ready",
        },
    },
    {
        "name": "validate_root", "title": "Use folder",
        "description": "Validate an absolute folder path.",
        "inputSchema": schema({"root": ROOT}, ["root"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "choose_folder", "title": "Choose folder",
        "description": "Open the native Windows folder chooser.",
        "inputSchema": schema({}),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "search_project", "title": "Search folder",
        "description": "Search UTF-8 text files with ripgrep when available.",
        "inputSchema": schema({
            "root": ROOT,
            "query": {"type": "string"},
            "caseSensitive": {"type": "boolean", "default": False},
            "regex": {"type": "boolean", "default": False},
            "wholeWord": {"type": "boolean", "default": False},
            "includeGlobs": {"type": ["string", "array"], "items": {"type": "string"}},
            "excludeGlobs": {"type": ["string", "array"], "items": {"type": "string"}},
            "maxResults": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
            "contextLines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
            "beforeContextLines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
            "afterContextLines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
        }, ["root", "query"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "search_files", "title": "Search file names",
        "description": "Find files by name or relative path within the selected folder.",
        "inputSchema": schema({
            "root": ROOT,
            "query": {"type": "string"},
            "caseSensitive": {"type": "boolean", "default": False},
            "regex": {"type": "boolean", "default": False},
            "wholeWord": {"type": "boolean", "default": False},
            "includeGlobs": {"type": ["string", "array"], "items": {"type": "string"}},
            "excludeGlobs": {"type": ["string", "array"], "items": {"type": "string"}},
            "maxResults": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
        }, ["root", "query"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "load_state", "title": "Restore YURICH preferences",
        "description": "Load the last local folder and search settings.",
        "inputSchema": schema({}),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "save_state", "title": "Remember YURICH preferences",
        "description": "Persist the current folder and search settings on this computer.",
        "inputSchema": schema({"state": {"type": "object"}}, ["state"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "read_file", "title": "Open file",
        "description": "Read a UTF-8 text file within the selected folder.",
        "inputSchema": schema({"root": ROOT, "path": FILE}, ["root", "path"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "save_file", "title": "Save file",
        "description": "Atomically save edits if the file has not changed on disk.",
        "inputSchema": schema({
            "root": ROOT, "path": FILE, "content": {"type": "string"},
            "expectedVersion": VERSION_SCHEMA,
        }, ["root", "path", "content", "expectedVersion"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "open_in_notepad", "title": "Open in Notepad",
        "description": "Open a file from the selected folder in Windows Notepad.",
        "inputSchema": schema({"root": ROOT, "path": FILE}, ["root", "path"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "git_status", "title": "Read Git status",
        "description": "Show the current branch and changed files for the selected repository.",
        "inputSchema": schema({"root": ROOT}, ["root"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "git_commit", "title": "Commit selected files",
        "description": "Create a local Git commit containing only the explicitly selected files.",
        "inputSchema": schema({
            "root": ROOT,
            "files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "message": {"type": "string", "minLength": 1, "maxLength": 10000},
        }, ["root", "files", "message"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "git_push", "title": "Push Git commits",
        "description": "Push the current branch to its upstream or create origin/<branch> upstream.",
        "inputSchema": schema({"root": ROOT}, ["root"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": True,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "list_package_scripts", "title": "List package scripts",
        "description": "List npm script names from package.json in the selected folder.",
        "inputSchema": schema({"root": ROOT}, ["root"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "start_command", "title": "Run project command",
        "description": "Run a command in the selected local folder after an explicit user action.",
        "inputSchema": schema({"root": ROOT, "command": {"type": "string"}}, ["root", "command"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": False, "openWorldHint": True,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "poll_command", "title": "Read command output",
        "description": "Read new output from a YURICH terminal command.",
        "inputSchema": schema({
            "processId": {"type": "string"},
            "cursor": {"type": "integer", "minimum": 0, "default": 0},
        }, ["processId"]),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"openai/widgetAccessible": True},
    },
    {
        "name": "stop_command", "title": "Stop project command",
        "description": "Stop a running YURICH terminal command and its child processes.",
        "inputSchema": schema({
            "processId": {"type": "string"},
            "cursor": {"type": "integer", "minimum": 0, "default": 0},
        }, ["processId"]),
        "annotations": {
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": True, "openWorldHint": False,
        },
        "_meta": {"openai/widgetAccessible": True},
    },
]

HANDLERS = {
    "validate_root": validate_root,
    "choose_folder": choose_folder,
    "search_project": search_project,
    "search_files": search_files,
    "load_state": load_state,
    "save_state": save_state,
    "read_file": read_file,
    "save_file": save_file,
    "open_in_notepad": open_in_notepad,
    "git_status": git_status,
    "git_commit": git_commit,
    "git_push": git_push,
    "list_package_scripts": list_package_scripts,
    "start_command": start_command,
    "poll_command": poll_command,
    "stop_command": stop_command,
}


def tool_result(data: dict[str, Any], message: str | None = None) -> dict[str, Any]:
    message = message or str(data.get("message") or data.get("status") or "Done")
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": data,
        "isError": data.get("status") in {"error", "conflict"},
    }


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "open_yurich":
        saved_state = load_state({}).get("state", {})
        return tool_result({
            "status": "ready",
            "root": str(arguments.get("root") or "").strip(),
            "savedState": saved_state,
            "message": "Choose a folder, enter a query, and search.",
        }, "YURICH opened.")
    handler = HANDLERS.get(name)
    if handler is None:
        raise YurichError(f"Unknown tool: {name}")
    return tool_result(handler(arguments))


def ui_resource(uri: str) -> dict[str, Any]:
    if uri not in UI_URIS:
        raise YurichError(f"Unknown resource: {uri}")
    html = (PLUGIN_ROOT / "ui" / "main.html").read_text(encoding="utf-8")
    return {"contents": [{
        "uri": uri,
        "mimeType": "text/html;profile=mcp-app",
        "text": html,
        "_meta": {"ui": {"prefersBorder": False}, "openai/widgetPrefersBorder": False},
    }]}


def handle(request: dict[str, Any]) -> None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method == "notifications/initialized" or request_id is None:
        return
    if method == "initialize":
        answer(request_id, {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
            "serverInfo": {"name": "yurich", "version": VERSION},
            "instructions": "Use open_yurich to launch the local search and editor UI.",
        })
    elif method == "ping":
        answer(request_id, {})
    elif method == "tools/list":
        answer(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise YurichError("Tool arguments must be an object.")
        answer(request_id, call_tool(str(params.get("name") or ""), arguments))
    elif method == "resources/list":
        answer(request_id, {"resources": [
            {
                "uri": UI_URI, "name": "YURICH interface",
                "description": "Local project search and quick editor.",
                "mimeType": "text/html;profile=mcp-app",
            },
            {
                "uri": UI_URIS[1], "name": "YURICH interface (compatibility)",
                "description": "Compatibility resource for existing conversations.",
                "mimeType": "text/html;profile=mcp-app",
            },
            {
                "uri": UI_URIS[2], "name": "YURICH interface (legacy compatibility)",
                "description": "Legacy compatibility resource.",
                "mimeType": "text/html;profile=mcp-app",
            },
        ]})
    elif method == "resources/templates/list":
        answer(request_id, {"resourceTemplates": []})
    elif method == "resources/read":
        answer(request_id, ui_resource(str(params.get("uri") or "")))
    else:
        fail(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id: Any = None
        request: dict[str, Any] | None = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request must be an object.")
            request_id = request.get("id")
            handle(request)
        except YurichError as error:
            if request_id is not None:
                if request and request.get("method") == "tools/call":
                    answer(request_id, tool_result({"status": "error", "message": str(error)}))
                else:
                    fail(request_id, -32000, str(error))
        except (json.JSONDecodeError, ValueError) as error:
            fail(request_id, -32700, str(error))
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            if request_id is not None:
                fail(request_id, -32603, f"Internal error: {error}")


if __name__ == "__main__":
    main()
