"""The music supplier: which remote MCP server we talk to, how we sign in, and one connection per tool call.

The supplier comes from suppliers.json (MUSIC_SUPPLIER picks the entry; MUSIC_MCP_URL / MUSIC_MCP_AUTH override it):
  oauth   - the supplier's own sign-in in the browser (Suno). Run login.py once; the token is kept in .tokens/
            and refreshed automatically. A tool call never opens a browser: without a token it says so.
  bearer  - MUSIC_MCP_TOKEN is sent as "Authorization: Bearer ..." (suppliers that hand out API keys).
  none    - no credentials.
Only https URLs: the token must never travel in clear text.
"""
import contextlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "suppliers.json"
AUTH_KINDS = ("oauth", "bearer", "none")
LOGIN_HINT = "run `python login.py` in jarvis/mcp-servers/music-mcp once (it opens the browser to sign in)"


class NotSignedIn(Exception):  # the message is safe to show
    pass


class SupplierError(Exception):  # the message is safe to show
    pass


def token_dir() -> Path:
    return Path(os.environ.get("MUSIC_TOKEN_DIR") or HERE / ".tokens")


def _https(url) -> str:
    url = str(url or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or len(url) > 500:
        raise ValueError(f"The supplier URL must be a plain https URL, not {url[:80]!r}")
    return url.rstrip("/")


def supplier(env=None, config=None) -> dict:
    """The chosen supplier entry: {key, name, url, auth, token, ops, site}. ValueError if the setup is wrong."""
    env = os.environ if env is None else env
    data = json.loads(CONFIG.read_text(encoding="utf-8")) if config is None else config
    entries = data.get("suppliers") or {}
    key = (env.get("MUSIC_SUPPLIER") or data.get("default") or "").strip()
    if key not in entries:
        raise ValueError(f"Unknown music supplier {key!r}; suppliers.json has: {', '.join(entries) or 'none'}")
    entry = dict(entries[key])
    auth = str(env.get("MUSIC_MCP_AUTH") or entry.get("auth") or "oauth").strip().lower()
    if auth not in AUTH_KINDS:
        raise ValueError(f"MUSIC_MCP_AUTH must be one of {', '.join(AUTH_KINDS)}, not {auth!r}")
    token = str(env.get("MUSIC_MCP_TOKEN") or "").strip() if auth == "bearer" else ""
    if auth == "bearer" and not token:
        raise ValueError(f"{entry.get('name') or key} uses an API key: set MUSIC_MCP_TOKEN")
    ops = entry.get("ops") or {}
    if not isinstance(ops, dict):
        raise ValueError("suppliers.json: ops must be an object")
    return {"key": key, "name": str(entry.get("name") or key), "url": _https(env.get("MUSIC_MCP_URL") or entry.get("url")),
            "auth": auth, "token": token, "ops": ops, "site": str(entry.get("site") or "")}


# ---------------------------------------------------------------- sign-in (OAuth)
def _store():
    from key_value.aio.stores.filetree import (FileTreeStore, FileTreeV1CollectionSanitizationStrategy,
                                               FileTreeV1KeySanitizationStrategy)
    folder = token_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return FileTreeStore(data_directory=folder,  # the sanitizers turn "https://..." keys into valid Windows file names
                         key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(folder),
                         collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(folder))


def _tokens(entry):
    from fastmcp.client.auth.oauth import TokenStorageAdapter
    return TokenStorageAdapter(async_key_value=_store(), server_url=entry["url"])


async def signed_in(entry) -> bool:  # a token is stored (it may still be refreshed or refused by the supplier)
    if entry["auth"] != "oauth":
        return True
    if not token_dir().exists():
        return False
    return await _tokens(entry).get_tokens() is not None


async def sign_out(entry):
    if token_dir().exists():
        await _tokens(entry).clear()


def open_browser(url) -> bool:
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:  # no browser on this machine, or it refused: the link is printed anyway
        return False


def _oauth(entry, interactive):
    from fastmcp.client.auth import OAuth
    from fastmcp.client.auth.oauth import ClientNotFoundError

    class SupplierOAuth(OAuth):
        async def redirect_handler(self, authorization_url):  # the step that opens the browser
            if not interactive:  # inside a tool call (Jarvis, Claude Code): never pop up a browser
                raise NotSignedIn(f"Signed out of {entry['name']} (the sign-in expired or was revoked): {LOGIN_HINT}")
            # fastmcp's own handler opens the browser silently: when that fails nothing shows. So: the same check
            # (a 400 = stale client registration -> fastmcp registers again and comes back here), then the link.
            async with self.httpx_client_factory() as http:
                answer = await http.get(authorization_url, follow_redirects=False)
            if answer.status_code == 400:
                raise ClientNotFoundError("OAuth client not found - cached credentials may be stale")
            if answer.status_code not in (200, 302, 303, 307, 308):
                raise SupplierError(f"{entry['name']}'s sign-in page answered HTTP {answer.status_code}")
            opened = open_browser(authorization_url)
            print(f"\n{'A browser window should open now. If it does not, open' if opened else 'Could not open the browser: open'}"
                  f" this link yourself (Ctrl+click, or copy it into the browser):\n\n{authorization_url}\n\n"
                  f"Sign in to {entry['name']} there and approve. Waiting up to 5 minutes...", flush=True)

    return SupplierOAuth(entry["url"], client_name="Jarvis music MCP", token_storage=_store())


def make_client(entry, interactive=False):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    auth = _oauth(entry, interactive) if entry["auth"] == "oauth" else (entry["token"] if entry["auth"] == "bearer" else None)
    # The browser sign-in happens while the session starts, so login.py gets the 5 minutes it waits for you
    return Client(StreamableHttpTransport(entry["url"], auth=auth), timeout=120, init_timeout=330 if interactive else 30)


# ---------------------------------------------------------------- errors
def _causes(error):  # the error, what caused it, and everything inside exception groups
    seen, todo = set(), [error]
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        todo += [current.__cause__, current.__context__, *getattr(current, "exceptions", ())]


def explain(error, entry) -> Exception:
    """Any failure while talking to the supplier -> NotSignedIn / SupplierError with a message for a person."""
    name = entry["name"]
    for cause in _causes(error):
        if isinstance(cause, (NotSignedIn, SupplierError)):
            return cause
    for cause in _causes(error):
        status = getattr(getattr(cause, "response", None), "status_code", None)
        if status in (401, 403):
            beta = f" Access may need an invite: {entry['site']}" if entry.get("site") else ""
            return SupplierError(f"{name} refused the connection (HTTP {status}): your account has no access yet, or the "
                                 f"sign-in is no longer valid ({LOGIN_HINT}).{beta}")
        if isinstance(status, int) and status >= 400:
            return SupplierError(f"{name} answered HTTP {status}")
    for cause in _causes(error):
        if type(cause).__name__ in ("ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException", "gaierror"):
            return SupplierError(f"Can't reach {name} at {entry['url']} ({type(cause).__name__}): check the internet connection")
    message = " ".join(str(error).split())[:300]
    return SupplierError(f"{name} MCP server failed ({type(error).__name__}{': ' + message if message else ''})")


# ---------------------------------------------------------------- the connection
class Upstream:
    """The supplier for this process. Tests pass entry= and connect= (a function returning an in-memory Client)."""

    def __init__(self, entry=None, connect=None):
        self.entry, self.connect = entry, connect
        self.tools = {"list": None, "url": None}  # the supplier's tool list, kept while this process runs

    def config(self):
        if self.entry is None:
            self.entry = supplier()
        return self.entry

    @contextlib.asynccontextmanager
    async def open(self, interactive=False):
        """One MCP session with the supplier. Every failure comes out as NotSignedIn or SupplierError."""
        entry = self.config()
        if not self.connect and not interactive and not await signed_in(entry):
            raise NotSignedIn(f"Not signed in to {entry['name']}: {LOGIN_HINT}")
        client = self.connect() if self.connect else make_client(entry, interactive)
        try:
            async with client:
                yield client
        except (NotSignedIn, SupplierError, ValueError):
            raise
        except Exception as error:
            raise explain(error, entry) from None

    async def list_tools(self, client):
        url = self.config()["url"]
        if self.tools["list"] is None or self.tools["url"] != url:
            found = await client.list_tools()
            self.tools.update(url=url, list=[{"name": t.name, "description": t.description or "",
                                              "input_schema": getattr(t, "input_schema", None) or {}} for t in found])
        return self.tools["list"]
