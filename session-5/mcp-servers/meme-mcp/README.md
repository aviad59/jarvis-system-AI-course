# memes MCP server

Free MCP server for memes, built on [Memegen](https://github.com/jacebrowning/memegen) (open source, MIT): it calls
the public Memegen API, so there is no key, no account, no Docker image and nothing to host. Hebrew works too.

| Tool | Returns |
|---|---|
| `list_templates(filter, animated, limit)` | Templates (`drake`, `fry`, `buzz`...) with how many text lines each takes |
| `get_template(template_id)` | One template's details |
| `generate_meme(template_id, text_lines, extension, font, style, layout)` | The meme: a Memegen image URL, or a local PNG for Hebrew |
| `generate_custom_meme(background_url, text_lines, extension, font)` | A meme on your own public image |
| `search_meme_examples(query, limit)` | Example memes for a word |

Write captions as normal text: spaces, `?`, `/`, `&`, `#`, `%` are escaped for you.

**Hebrew:** Memegen's fonts have no Hebrew letters (they come out as empty boxes), so memes with Hebrew text are
drawn here with Pillow, on the template's blank image, at the text boxes from Memegen's own template config, right to
left (English words and numbers inside keep their order). The font is Arial Bold; set `MEME_FONT` to another `.ttf`.

## Based on lofcz/memegen-mcp, after a security review

The tool set follows [lofcz/memegen-mcp](https://github.com/lofcz/memegen-mcp). It was read in full first (never
installed or run). No malicious code: no obfuscation, no hidden binaries, no data sent anywhere but the Memegen API.
Its weaknesses are why this is a rewrite and not a copy:

| In lofcz/memegen-mcp | Here |
|---|---|
| `generate_custom_meme` passes any URL to the Memegen server, which fetches it: with a self-hosted Memegen, a prompt-injected model can reach localhost or the local network (server-side request forgery) | only public `http(s)` addresses; localhost, private, link-local (`169.254.x`) and URLs with credentials are refused |
| text and template ids go into the URL path unescaped (`/`, `?`, `#` change the request) | Memegen's escapes; ids, fonts, styles and extensions are validated names |
| runs a prebuilt, unauditable Docker image (`lofcz1/memegen-mcp`) with unpinned dependencies | no image: the public API, or your own `MEMEGEN_BASE_URL` |
| logs every call's arguments | no argument logging |

`python check_server.py` tests these checks (no network needed).

## Setup

The Python that runs Jarvis already works if it has the packages: `python -m pip install -r requirements.txt`.
Or a separate venv in this folder (`.venv`), which Jarvis then uses automatically.

## Used by
- **Claude Code**: `.mcp.json` in the repo root registers it as `memes`. Approve it, then check with `/mcp`.
- **Jarvis**: *Ask Jarvis* sends "make a meme about ..." / "תעשה לי מם על ..." to the **Memes** skill
  (`session-5/jarvis-rag/jarvis/skills.py`), which runs `jarvis_client.py` to call these tools over MCP stdio.
  If you name a template and write the captions (`drake meme: writing tests / shipping on friday`) no model is needed;
  otherwise Jarvis first searches your notes for the topic (retrieval only) and the local Ollama model writes
  the captions from what the notes say, in your language.

| Setting | Default | Meaning |
|---|---|---|
| `MEMEGEN_BASE_URL` | `https://api.memegen.link` | Memegen API to use |
| `MEME_FONT` | Arial Bold | font for Hebrew memes (must include Hebrew letters) |
| `MEME_OUTPUT_DIR` | `meme-mcp/output/` | where Hebrew memes are saved |

Privacy: the caption text is sent to the public Memegen API (English memes) or stays on this computer (Hebrew memes).
