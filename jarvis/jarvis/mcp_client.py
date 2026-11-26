"""MCP client for Jarvis: one long-lived stdio connection per MCP server, shared by every skill.

The servers come from jarvis/.mcp.json (the same file Claude Code reads). Each one is started on first use and
then kept running, so yt-dlp / Pillow load once, not on every tool call. A server that died is restarted once.
Everything closes when Jarvis exits.

  import mcp_client
  mcp_client.servers()                                    -> ["youtube-shorts", "memes"]
  mcp_client.list_tools("memes")                          -> [{"name", "description", "input_schema"}, ...]
  mcp_client.call_tool("memes", "generate_meme", {"template_id": "drake", "text_lines": ["a", "b"]})  -> the tool's JSON

Standard library only: Jarvis's Python doesn't need fastmcp (each server has it in its own venv). The protocol is
JSON-RPC 2.0, one JSON message per line on the server's stdin / stdout: initialize -> notifications/initialized ->
tools/list, tools/call. The server's stderr is its log.
"""
import atexit  # close the servers when Jarvis exits
import collections  # recent stderr lines, for error messages
import itertools  # request ids
import json  # JSON-RPC messages and .mcp.json
import os  # venv layout, environment
import subprocess  # the server processes
import sys  # fallback Python
import threading  # reader threads, locks
import time  # timeouts
from pathlib import Path  # paths in .mcp.json

ROOT = Path(__file__).resolve().parents[1]  # the jarvis/ folder: paths in .mcp.json are relative to it
CONFIG = ROOT / ".mcp.json"
PROTOCOL_VERSION = "2025-11-25"
CONNECT_TIMEOUT = 60  # starting a server (imports, first run of a venv) can take a while
POLL = 0.1  # how often a waiting call checks cancelled()


class McpError(RuntimeError):  # the message is safe to show in the console
    pass


class Cancelled(Exception):  # cancelled() returned True while waiting; the skill then calls run.check_cancelled()
    pass


class _Lost(Exception):  # the server process ended: retry once on a new one
    pass


def _venv_python(folder):
    venv = Path(folder) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else None


def resolve(spec, root=ROOT):
    """A .mcp.json entry -> (argv, cwd, env). A Python server runs with its own .venv if it has one, else this Python."""
    args = [str(a) for a in spec.get("args", [])]
    script = next((a for a in args if a.endswith(".py")), None)
    folder = (root / script).parent if script else root
    command = str(spec["command"])
    if Path(command).name.lower().startswith("python") and script:  # "python", ".venv/Scripts/python.exe", ...
        command = _venv_python(folder) or sys.executable
    elif not Path(command).is_absolute() and (root / command).exists():
        command = str(root / command)
    args = [str(root / a) if not Path(a).is_absolute() and (root / a).exists() else a for a in args]
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()},
           "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    return [command, *args], str(folder), env


class Connection:  # one running server process
    def __init__(self, name, argv, cwd, env):
        self.name, self.argv = name, argv
        self.ids = itertools.count(1)
        self.pending = {}  # request id -> {"done": Event, "message": response}
        self.lock = threading.Lock()  # pending
        self.write_lock = threading.Lock()  # one message at a time on stdin
        self.log = None  # where the server's stderr goes: the latest call's log (kept, so lines that arrive
        #                  just after the answer aren't lost)
        self.stderr = collections.deque(maxlen=20)
        self.tools = None  # list_tools() cache, for this process
        self.closed = False
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, creationflags=flags)
        except OSError as error:
            raise McpError(f"Can't start the {name} MCP server ({argv[0]}): {error}") from None
        threading.Thread(target=self._read_stdout, daemon=True, name=f"mcp-{name}-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name=f"mcp-{name}-err").start()

    def alive(self):
        return not self.closed and self.process.poll() is None

    # -- reading -------------------------------------------------------------
    def _read_stdout(self):
        try:
            for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").strip()
                try:
                    self._on_line(line)
                except Exception:  # one odd message never stops the reader
                    pass
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True  # EOF: the process ended; wake every waiting call
            self.process.stdout.close()
            with self.lock:
                waiters = list(self.pending.values())
            for waiter in waiters:
                waiter["done"].set()

    def _on_line(self, line):
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError:  # not protocol: a stray print in the server
            return self._emit(line)
        if not isinstance(message, dict):
            return
        if "method" in message:
            return self._on_server_message(message)
        with self.lock:
            waiter = self.pending.get(message.get("id"))
        if waiter:  # an answer to a call we gave up on (cancel, timeout) is dropped
            waiter["message"] = message
            waiter["done"].set()

    def _read_stderr(self):
        try:
            for raw in self.process.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr.append(line)
                    self._emit(line)
        except (OSError, ValueError):
            pass
        finally:
            self.process.stderr.close()

    def _emit(self, line):
        log = self.log
        if log:
            try:
                log(line)
            except Exception:
                pass

    def _on_server_message(self, message):
        if message.get("method") == "notifications/message":  # the server's own log
            params = message.get("params") or {}
            data = params.get("data")
            self._emit(f"[{params.get('level', 'info')}] {data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)}")
        elif "id" in message:  # a request to us: ping is the only one a client must answer
            if message["method"] == "ping":
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            else:
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": f"Jarvis doesn't support {message['method']}"}})

    # -- writing -------------------------------------------------------------
    def _send(self, message):
        data = (json.dumps(message) + "\n").encode("utf-8")  # ASCII JSON: one line, no encoding surprises
        try:
            with self.write_lock:
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError):  # broken pipe / closed: the process is gone
            self.closed = True
            raise _Lost() from None

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(self, method, params, timeout, cancelled=None):
        """Send one request and wait for its answer; returns the 'result' object."""
        if not self.alive():
            raise _Lost()
        request_id = next(self.ids)
        waiter = {"done": threading.Event(), "message": None}
        with self.lock:
            self.pending[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while not waiter["done"].wait(POLL):
                if cancelled and cancelled():
                    self._cancel(request_id, "cancelled by the user")
                    raise Cancelled(f"{method} on {self.name} was cancelled")
                if time.monotonic() > deadline:
                    self._cancel(request_id, "timed out")
                    raise TimeoutError()
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
        message = waiter["message"]
        if message is None:  # woken by EOF
            raise _Lost()
        if "error" in message:
            error = message["error"] or {}
            raise McpError(str(error.get("message") or error))
        return message.get("result") or {}

    def _cancel(self, request_id, reason):
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except _Lost:
            pass

    def handshake(self, cancelled=None):
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": {"name": "jarvis", "version": "1.0"}},
                              CONNECT_TIMEOUT, cancelled)
        self.notify("notifications/initialized")
        return result

    def close(self, wait=2):
        self.closed = True
        try:
            self.process.stdin.close()  # EOF on stdin: a stdio MCP server exits on its own
        except OSError:
            pass
        try:
            self.process.wait(wait)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(wait)
            except subprocess.TimeoutExpired:
                pass


class Client:
    """The MCP servers of one .mcp.json (or a dict like its "mcpServers"), each with one shared connection."""

    def __init__(self, config=CONFIG, root=None):
        if isinstance(config, dict):
            self.specs, self.root = dict(config), Path(root or ROOT)
        else:
            self.specs, self.root, self.config = None, Path(root or Path(config).parent), Path(config)
        self.connections = {}
        self.locks = {}
        self.lock = threading.Lock()

    def _specs(self):
        if self.specs is None:
            try:
                data = json.loads(self.config.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise McpError(f"No MCP servers: {self.config} is missing") from None
            except ValueError as error:
                raise McpError(f"{self.config.name} is not valid JSON: {error}") from None
            self.specs = {name: spec for name, spec in (data.get("mcpServers") or {}).items()
                          if spec.get("type", "stdio") == "stdio" and spec.get("command")}
        return self.specs

    def servers(self):
        return list(self._specs())

    def _connect(self, server, log, cancelled):  # the server's live connection; starts it if needed
        specs = self._specs()
        if server not in specs:
            raise McpError(f"Unknown MCP server {server!r}; .mcp.json has: {', '.join(specs) or 'none'}")
        with self.lock:
            lock = self.locks.setdefault(server, threading.Lock())
        with lock:  # one start at a time per server; other servers aren't blocked
            connection = self.connections.get(server)
            if connection and connection.alive():
                return connection
            if connection:
                connection.close(wait=0.5)
                self._say(log, f"MCP: the {server} server stopped, starting it again")
            argv, cwd, env = resolve(specs[server], self.root)
            connection = Connection(server, argv, cwd, env)
            connection.log = log
            try:
                connection.handshake(cancelled)
                connection.tools = self._fetch_tools(connection)
            except (_Lost, TimeoutError) as error:
                connection.close(wait=0.5)
                raise McpError(self._failure(connection, "didn't start" if isinstance(error, _Lost)
                                             else f"didn't answer within {CONNECT_TIMEOUT} s")) from None
            except BaseException:
                connection.close(wait=0.5)
                raise
            self.connections[server] = connection
            self._say(log, f"MCP: connected to {server}, tools: {', '.join(t['name'] for t in connection.tools)}")
            return connection

    @staticmethod
    def _fetch_tools(connection):
        tools, cursor = [], None
        while True:
            page = connection.request("tools/list", {"cursor": cursor} if cursor else {}, CONNECT_TIMEOUT)
            tools += [{"name": t["name"], "description": t.get("description") or "",
                       "input_schema": t.get("inputSchema") or {}, "_output_schema": t.get("outputSchema") or {}}
                      for t in page.get("tools", [])]
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    @staticmethod
    def _failure(connection, what):
        last = next((line for line in reversed(connection.stderr) if line.strip()), "")
        return f"The {connection.name} MCP server {what}" + (f": {last}" if last else f" ({connection.argv[0]})")

    @staticmethod
    def _say(log, line):
        if log:
            log(line)

    def _with_connection(self, server, log, cancelled, work):  # run work(connection); one retry on a new process
        for attempt in (1, 2):
            connection = self._connect(server, log, cancelled)
            try:
                return work(connection)
            except _Lost:  # _connect() says "starting it again" on the retry
                if attempt == 2:
                    raise McpError(self._failure(connection, "stopped during the call")) from None

    def list_tools(self, server, *, log=None, cancelled=None):
        tools = self._with_connection(server, log, cancelled, lambda c: c.tools)
        return [{k: v for k, v in t.items() if not k.startswith("_")} for t in tools]

    def call_tool(self, server, tool, arguments=None, *, log=None, cancelled=None, timeout=120):
        arguments = dict(arguments or {})

        def work(connection):
            schema = next((t["_output_schema"] for t in connection.tools if t["name"] == tool), None)
            if schema is None:
                raise McpError(f"tool {tool!r} is not offered by the {server} MCP server")
            self._say(log, f"MCP: call {tool} {json.dumps(arguments, ensure_ascii=False)}")
            connection.log = log
            try:
                result = connection.request("tools/call", {"name": tool, "arguments": arguments}, timeout, cancelled)
            except TimeoutError:
                connection.close(wait=0.5)  # a stuck server is restarted on the next call
                raise McpError(f"The {server} MCP server didn't answer {tool} within {timeout} s") from None
            return _tool_data(tool, result, schema)
        return self._with_connection(server, log, cancelled, work)

    def close(self):
        with self.lock:
            connections, self.connections = list(self.connections.values()), {}
        for connection in connections:
            connection.close()


def _tool_data(tool, result, schema):  # a tools/call result -> the tool's JSON (like fastmcp's result.data)
    texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    if result.get("isError"):
        message = (texts[0] if texts else "") or "the tool failed"
        raise McpError(message.removeprefix(f"Error calling tool {tool!r}: "))
    data = result.get("structuredContent")
    if data is not None:
        wrapped = ((result.get("_meta") or {}).get("fastmcp") or {}).get("wrap_result") or schema.get("x-fastmcp-wrap-result")
        return data.get("result") if wrapped and isinstance(data, dict) else data
    text = "".join(texts)
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


DEFAULT = Client()  # the one Jarvis uses; tests may swap it for a Client of fake servers


def servers():
    return DEFAULT.servers()


def list_tools(server, *, log=None, cancelled=None):
    return DEFAULT.list_tools(server, log=log, cancelled=cancelled)


def call_tool(server, tool, arguments=None, *, log=None, cancelled=None, timeout=120):
    return DEFAULT.call_tool(server, tool, arguments, log=log, cancelled=cancelled, timeout=timeout)


def close():
    DEFAULT.close()


atexit.register(lambda: DEFAULT.close())  # whichever Client is DEFAULT at exit
