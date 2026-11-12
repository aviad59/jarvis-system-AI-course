"""MCP client that Jarvis runs as a child process: connect to server.py over stdio, call one tool, save the result.

  python jarvis_client.py --tool generate_meme --args '{"template_id": "drake", "text_lines": ["a", "b"]}' --output r.json

Progress goes to stdout (Jarvis streams it into the console log); the tool's JSON result goes to --output.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client

SERVER = Path(__file__).resolve().parent / "server.py"


async def main(tool: str, arguments: dict, output: Path) -> int:
    async with Client(SERVER) as client:  # .py path -> stdio transport, launched with this Python
        tools = [t.name for t in await client.list_tools()]
        print(f"MCP: connected to memes, tools: {', '.join(tools)}", flush=True)
        if tool not in tools:
            print(f"ERROR: tool {tool!r} is not offered by the server", flush=True)
            return 1
        print(f"MCP: call {tool} {json.dumps(arguments, ensure_ascii=False)}", flush=True)
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    if result.is_error:
        message = result.content[0].text if result.content else "the tool failed"
        output.write_text(json.dumps({"ok": False, "error": message}, ensure_ascii=False), encoding="utf-8")
        print(f"ERROR: {message.removeprefix(f'Error calling tool {tool!r}: ')}", flush=True)
        return 1
    data = result.data if result.data is not None else result.structured_content
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if not data or not data.get("ok"):
        print(f"ERROR: {(data or {}).get('error', 'the tool returned no result')}", flush=True)
        return 1
    if data.get("url") or data.get("file"):
        print(f"MCP: meme ready ({data.get('renderer')}): {data.get('url') or data.get('file')}", flush=True)
    else:
        print(f"MCP: {data.get('count', 0)} result(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args", default="{}", help="tool arguments as JSON")
    parser.add_argument("--output", required=True, type=Path)
    a = parser.parse_args()
    sys.exit(asyncio.run(main(a.tool, json.loads(a.args), a.output)))
