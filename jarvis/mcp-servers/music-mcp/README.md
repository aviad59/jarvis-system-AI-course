# music MCP server

Songs through any music supplier's MCP server. The default supplier is **Suno's official MCP server** (beta,
`https://api.suno.com/mcp`): you sign in with your Suno account, songs use your Suno credits, and there is no API key.
Access is early-access for now; sign up at https://suno.com/mcp.

Suno's server is remote and uses an OAuth sign-in, but Jarvis's MCP client only speaks stdio. So this server runs locally
over stdio, signs in once, and maps a **fixed set of tools** onto whatever the supplier offers. If you switch supplier,
only `suppliers.json` changes; Jarvis and Claude Code keep calling the same tools.

| Tool | Returns |
|---|---|
| `create_song(description, lyrics, style, title, instrumental, wait)` | `{ok, status: ready\|pending\|failed, songs: [{id, title, audio_url, image_url, page_url, duration, ...}]}`. With `wait` (the default) it waits up to `MUSIC_WAIT_SECONDS` for the songs to be ready |
| `get_song(song_id)` | One song's status and links |
| `list_songs(limit)` | Your latest songs |
| `get_credits()` | Credits left |
| `supplier_tools()` | The supplier's own tools and which of them each tool above uses |

## Setup (once)
```
cd jarvis/mcp-servers/music-mcp
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe login.py          # the browser opens: sign in, approve; prints the supplier's tools
```
The token is kept in `.tokens/` (git-ignored) and refreshed automatically. To forget it, run `login.py --logout`.
A tool call never opens a browser: without a sign-in it returns "Not signed in ... run login.py".

## Switching supplier / fixing the mapping
`mapping.py` matches our tools to the supplier's tools by name (`generate_music` becomes `create_song`, and so on). Each of
our arguments is matched to the supplier's arguments by name (`description` becomes `prompt`, `style` becomes `tags`).
What has no field (style, title, instrumental) is written into the description. To pin a match, add it to `suppliers.json`:
```json
"ops": {"create_song": {"tool": "generate_music", "args": {"description": "prompt"}, "fixed": {"model": "v5"}},
        "get_song": "get_music_status"}
```
A new supplier is a new entry under `suppliers`, with `"auth"` set to `oauth`, `bearer` (key in `MUSIC_MCP_TOKEN`) or `none`.

| Setting | Default | Meaning |
|---|---|---|
| `MUSIC_SUPPLIER` | `suno` | entry of `suppliers.json` |
| `MUSIC_MCP_URL` / `MUSIC_MCP_AUTH` | from the entry | override the URL (https only) / auth |
| `MUSIC_MCP_TOKEN` | | API key for `bearer` suppliers |
| `MUSIC_WAIT_SECONDS` / `MUSIC_POLL_SECONDS` | 240 / 5 | how long `create_song` waits, and how often it checks |
| `MUSIC_TOKEN_DIR` | `.tokens/` | where the sign-in is kept |

Claude Code alone could also add Suno directly (`claude mcp add --transport http suno https://api.suno.com/mcp`).
`music` in `jarvis/.mcp.json` serves both Claude Code and Jarvis.

`python check_server.py` runs the tests against a fake supplier (no network).
Privacy: what you describe (and any lyrics) is sent to the supplier. The token never leaves this folder.
