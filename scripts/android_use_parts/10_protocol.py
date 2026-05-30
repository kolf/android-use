# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def tool_descriptors() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": metadata["description"],
            "inputSchema": metadata["inputSchema"],
        }
        for name, metadata in TOOLS.items()
    ]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": tool_descriptors()}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"content": [text_content(f"Unknown tool: {name}")], "isError": True},
            }
        try:
            maybe_show_scrcpy_for_tool_call(name, arguments)
            content = TOOLS[name]["handler"](arguments)
            return {"jsonrpc": "2.0", "id": message_id, "result": {"content": content}}
        except Exception as exc:  # Keep MCP session alive on tool errors.
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"content": [text_content(str(exc))], "isError": True},
            }

    if message_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    start_resident_scrcpy_monitor()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
            if response is not None:
                send(response)
        except Exception as exc:
            error = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Invalid request: {exc}"},
            }
            send(error)


if __name__ == "__main__":
    main()
