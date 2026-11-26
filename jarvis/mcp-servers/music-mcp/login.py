"""Sign in to the music supplier once (Suno: your Suno account), then see what its MCP server offers.

  python login.py            sign in (opens the browser: approve the connection), then list the supplier's tools
  python login.py --logout   forget the sign-in

The token is saved in .tokens/ (git-ignored) and refreshed automatically, so server.py can call the supplier without
a browser. It is never printed. Run it with the folder's venv: .venv/Scripts/python.exe login.py
"""
import asyncio
import sys

import mapping
import upstream


async def main(argv) -> int:
    try:
        entry = upstream.supplier()
    except ValueError as error:
        print(f"Setup problem: {error}")
        return 2
    if "--logout" in argv:
        await upstream.sign_out(entry)
        print(f"Signed out of {entry['name']} ({entry['url']}).")
        return 0
    if entry["auth"] == "oauth":
        print(f"Signing in to {entry['name']} ({entry['url']})...", flush=True)
    else:
        print(f"{entry['name']} uses auth '{entry['auth']}': no sign-in needed, checking the connection.")
    link = upstream.Upstream(entry)
    try:
        async with link.open(interactive=True) as client:
            tools = await link.list_tools(client)
    except (upstream.NotSignedIn, upstream.SupplierError, ValueError) as error:
        print(f"Failed: {error}")
        return 1
    print(f"\nSigned in and connected. {entry['name']} offers {len(tools)} tool(s):")
    for tool in tools:
        arguments = ", ".join(sorted((tool["input_schema"].get("properties") or {})))
        print(f"  - {tool['name']}({arguments}): {' '.join(tool['description'].split())[:160]}")
    print("\nOur tools use:")
    for op in mapping.OPS:
        try:
            chosen = mapping.plan(op, tools, entry["ops"])["tool"]["name"]
        except LookupError:
            chosen = None
        print(f"  {op:12} -> {chosen or 'nothing (map it in suppliers.json under ops)'}")
    print("\nIf a match is wrong, pin it in suppliers.json, e.g. \"ops\": {\"create_song\": \"<tool name>\"}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
