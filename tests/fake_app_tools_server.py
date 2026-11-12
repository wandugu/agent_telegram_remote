"""Stand-in for codex-app-tools server.mjs: newline-delimited JSON-RPC over stdio.

Mirrors the real app: browser-use pipes answer initialize but reject tools/list,
and every app tool call must carry executor thread metadata.
"""

from __future__ import annotations

import json
import os
import sys

LOG = os.environ.get("FAKE_APP_TOOLS_LOG")
THREAD_STATUS = os.environ.get("FAKE_THREAD_STATUS")  # read_thread answers with this thread status type
PIPE = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
APP_PIPE = not PIPE.endswith("bad")
TOOLS = ["list_threads", "read_thread", "navigate_to_codex_page", "send_message_to_thread"]


def reply(message: dict) -> None:
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def error(request_id: object, code: int, message: str) -> None:
    reply({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


for raw in sys.stdin.buffer:
    message = json.loads(raw.decode("utf-8"))
    if LOG:
        with open(LOG, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pipe": PIPE, **message}, ensure_ascii=False) + "\n")
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        reply({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                               "serverInfo": {"name": "fake", "version": "0"}}})
    elif not APP_PIPE:
        error(request_id, -1, f"MCP error -1: No handler registered for method: {method}")
    elif method == "tools/list":
        reply({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": name} for name in TOOLS]}})
    elif method == "tools/call":
        params = message["params"]
        if not (params.get("_meta") or {}).get("openai/threadId"):
            error(request_id, -32602, "MCP error -32602: Codex app tools require thread metadata from the executor.")
            continue
        reply({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
        if params["arguments"].get("prompt") == "FAIL":
            result = {"isError": True, "content": [{"type": "text", "text": "rejected"}]}
            reply({"jsonrpc": "2.0", "id": request_id, "result": result})
        elif params["name"] == "read_thread" and THREAD_STATUS:
            text = json.dumps({"thread": {"id": params["arguments"]["threadId"], "status": {"type": THREAD_STATUS}},
                               "turns": [{"status": "inProgress" if THREAD_STATUS == "active" else "interrupted"}]})
            reply({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}]}})
        else:
            text = json.dumps({"ok": True, "tool": params["name"]})
            reply({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}]}})
    else:
        error(request_id, -32601, f"unknown {method}")
