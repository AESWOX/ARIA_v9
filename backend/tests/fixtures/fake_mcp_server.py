"""Фейковый MCP-сервер (stdio) для тестов H7. Намеренно «шумный»: пишет в stdout мусор и шлёт ping."""
import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_thing", "description": "Writes a thing", "inputSchema": {"type": "object", "properties": {"v": {"type": "string"}}},
     "annotations": {"readOnlyHint": False}},
    {"name": "reader_note", "description": "Name suggests read, but no annotation", "inputSchema": {"type": "object", "properties": {}}},
]
PAGES = [TOOLS[:2], TOOLS[2:]]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def text_result(mid, text, **extra):
    send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], **extra}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            continue  # ответ клиента на наш ping
        if method == "initialize":
            sys.stdout.write("starting up... (not json)\n")
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                              "serverInfo": {"name": "fake", "version": "0.0.1"}}})
            send({"jsonrpc": "2.0", "id": 9001, "method": "ping"})
        elif method == "tools/list":
            cursor = (msg.get("params") or {}).get("cursor")
            page = 1 if cursor == "p2" else 0
            result = {"tools": PAGES[page]}
            if page == 0:
                result["nextCursor"] = "p2"
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                text_result(mid, f"echo:{args.get('text', '')}")
            elif name == "write_thing":
                text_result(mid, f"wrote:{args.get('v', '')}", structuredContent={"ok": True})
            elif name == "big":
                text_result(mid, "x" * 30000)
            elif name == "boom":
                text_result(mid, "kaboom", isError=True)
            elif name == "sleepy":
                time.sleep(3)
                text_result(mid, "late")
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool: {name}"}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
