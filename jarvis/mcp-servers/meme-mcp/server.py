# Free MCP server for memes, built on the open-source Memegen API (github.com/jacebrowning/memegen, MIT).
#
# Same tools as github.com/lofcz/memegen-mcp, which was audited first and is NOT used or copied. This version
# fixes what the audit found:
#   * meme text is escaped with Memegen's own rules and every id / font / style / extension is validated,
#     so text can't break out of the URL path (the original pasted it into the URL as-is);
#   * a custom background must be a public http(s) address; localhost and private-network addresses are
#     refused (the original let the Memegen server fetch any URL: server-side request forgery);
#   * no prebuilt Docker image and no self-hosted server: it calls the public API (MEMEGEN_BASE_URL to change);
#   * no logging of every call's arguments.
# Hebrew: the public API's fonts have no Hebrew letters (they render as empty boxes), so memes with Hebrew text
# are drawn here with Pillow, on the template's blank image, at the text positions from Memegen's own template
# config, right to left. Nothing is sent anywhere but the Memegen API (and GitHub, for those template layouts).
import functools
import hashlib
import ipaddress
import os
import re
import socket
import sys
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastmcp import FastMCP

mcp = FastMCP("memes")
NAME = "memes"

def log(message):  # stderr is the server's log (stdout is the MCP channel): Jarvis shows it in the Mission log
    print(f"{NAME}: {message}", file=sys.stderr, flush=True)


def logged(tool):  # each call's start, outcome and time in the log (the arguments are in Jarvis's "MCP: call" line)
    @functools.wraps(tool)
    def run(*args, **kwargs):
        started = time.monotonic()
        log(f"{tool.__name__} ...")
        try:
            result = tool(*args, **kwargs)
        except Exception as error:
            log(f"{tool.__name__} failed in {time.monotonic() - started:.1f}s: {type(error).__name__}: "
                f"{' '.join(str(error).split())[:200]}")
            raise
        seconds = time.monotonic() - started
        if isinstance(result, dict) and not result.get("ok", True):
            log(f"{tool.__name__} failed in {seconds:.1f}s: {result.get('error')}")
        else:
            count = result.get("count") if isinstance(result, dict) else None
            log(f"{tool.__name__} done in {seconds:.1f}s{f': {count} result(s)' if count is not None else ''}")
        return result
    return run

BASE = os.environ.get("MEMEGEN_BASE_URL", "https://api.memegen.link").rstrip("/")
LAYOUTS_URL = "https://raw.githubusercontent.com/jacebrowning/memegen/main/templates/{id}/config.yml"
OUTPUT = Path(os.environ.get("MEME_OUTPUT_DIR") or Path(__file__).resolve().parent / "output")
TIMEOUT = 20
MAX_LINES, MAX_LINE_CHARS, MAX_DOWNLOAD = 10, 200, 15 * 1024 * 1024
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
SAFE_WORD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")  # fonts and styles: a name, never a URL
EXTENSIONS = ("png", "jpg", "gif", "webp")
LAYOUT_CHOICES = ("default", "top")
HEBREW = re.compile(r"[\u0590-\u05FF]")
# Memegen's URL escapes (https://memegen.link/#special-characters); "_" and "-" first, they become spaces.
ESCAPES = [("_", "__"), ("-", "--"), (" ", "_"), ("?", "~q"), ("&", "~a"), ("%", "~p"), ("#", "~h"), ("/", "~s"),
           ("\\", "~b"), ("<", "~l"), (">", "~g"), ('"', "''"), ("\n", "~n")]
FONT_FILES = [os.environ.get("MEME_FONT", ""), "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/tahomabd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]  # bold fonts that include Hebrew letters
_cache = {"templates": None, "at": 0.0, "layouts": {}}


# ---------------------------------------------------------------- input checks
def _template_id(value) -> str:
    value = str(value or "").strip().lower()
    if not TEMPLATE_ID.match(value):
        raise ValueError(f"Invalid template id {value!r}: use an id from list_templates, e.g. 'drake'")
    return value


def _lines(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("text_lines must be a list of strings")
    lines = [str(line).strip()[:MAX_LINE_CHARS] for line in value][:MAX_LINES]
    if not any(lines):
        raise ValueError("text_lines needs at least one non-empty line")
    return lines


def _choice(value, name, allowed=None):
    value = str(value or "").strip().lower()
    if not value:
        return None
    if (allowed and value not in allowed) or (not allowed and not SAFE_WORD.match(value)):
        raise ValueError(f"Invalid {name} {value!r}" + (f": one of {', '.join(allowed)}" if allowed else ""))
    return value


def _public_url(url) -> str:
    """Only public http(s) addresses: refuses localhost, private networks, link-local and credentials in the URL."""
    url = str(url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or len(url) > 1000:
        raise ValueError("background_url must be a plain public http(s) URL")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except socket.gaierror:
        raise ValueError(f"Cannot resolve {parts.hostname}") from None
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise ValueError("background_url points to a private or local address; only public images are allowed")
    return url


def _encode(line: str) -> str:
    if not line:
        return "_"  # an empty line keeps its place
    for plain, escaped in ESCAPES:
        line = line.replace(plain, escaped)
    return quote(line, safe="_-~'")


# ---------------------------------------------------------------- Memegen API
def _get(client, path, **params):
    response = client.get(BASE + path, params=params or None)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _templates(client) -> list[dict]:
    if _cache["templates"] is None or time.time() - _cache["at"] > 3600:
        _cache.update(templates=_get(client, "/templates/") or [], at=time.time())
    return _cache["templates"]


def _summary(t: dict) -> dict:
    return {"id": t["id"], "name": t["name"], "lines": t["lines"], "styles": t.get("styles") or [],
            "keywords": t.get("keywords") or [], "example": t["example"]["url"], "blank": t["blank"]}


def _download(client, url) -> bytes:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        data = b""
        for chunk in response.iter_bytes():
            data += chunk
            if len(data) > MAX_DOWNLOAD:
                raise ValueError("Image is too large")
    return data


# ---------------------------------------------------------------- local renderer (Hebrew)
_STRONG_LTR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,:/_%+'-]*[A-Za-z0-9%]|[A-Za-z0-9]")
_MIRROR = str.maketrans("()[]{}<>", ")(][}{><")


def visual_order(line: str) -> str:
    """Right-to-left line -> left-to-right drawing order (Pillow here has no bidi support).
    Latin/number runs ("Cedar", "SYS-001", "94%") keep their order; Hebrew runs are reversed, brackets mirrored."""
    if not HEBREW.search(line):
        return line
    runs, last = [], 0
    for match in _STRONG_LTR.finditer(line):
        if match.start() > last:
            runs.append(("rtl", line[last:match.start()]))
        runs.append(("ltr", match.group()))
        last = match.end()
    if last < len(line):
        runs.append(("rtl", line[last:]))
    return "".join(text if kind == "ltr" else text[::-1].translate(_MIRROR) for kind, text in reversed(runs))


def _layout(client, template_id) -> list[dict]:
    """Text boxes from Memegen's template config (relative positions); top/bottom when it can't be read."""
    if template_id not in _cache["layouts"]:
        boxes = []
        try:
            import yaml
            response = client.get(LAYOUTS_URL.format(id=template_id))
            if response.status_code == 200 and len(response.content) < 200_000:
                boxes = (yaml.safe_load(response.text) or {}).get("text") or []
        except Exception:
            boxes = []
        _cache["layouts"][template_id] = boxes or [
            {"anchor_x": 0.0, "anchor_y": 0.0, "scale_x": 1.0, "scale_y": 0.2},
            {"anchor_x": 0.0, "anchor_y": 0.8, "scale_x": 1.0, "scale_y": 0.2}]
    return _cache["layouts"][template_id]


def _font(size):
    from PIL import ImageFont
    for path in FONT_FILES:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError("No font with Hebrew letters found; set MEME_FONT to a .ttf file (e.g. Arial Bold)")


def _fit(draw, text, width, height):
    """Largest font size at which the wrapped text fits the box; returns (font, wrapped logical lines)."""
    words = text.split()
    for size in range(max(12, int(height * 0.9)), 9, -2):
        font = _font(size)
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and draw.textlength(trial, font=font) > width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
        line_height = size * 1.15
        if len(lines) * line_height <= height and all(draw.textlength(line, font=font) <= width for line in lines):
            return font, lines
    return _font(10), [text]


def render_locally(client, template: dict, lines: list[str], layout: str | None) -> Path:
    from PIL import Image, ImageColor, ImageDraw
    Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs
    blank = template["blank"]
    if urlsplit(blank).hostname != urlsplit(BASE).hostname:
        raise ValueError("Unexpected blank image host")
    image = Image.open(BytesIO(_download(client, blank)))
    image.seek(0)  # animated templates: first frame
    image = image.convert("RGBA")
    width, height = image.size
    boxes = _layout(client, template["id"])
    if layout == "top" and len(boxes) >= 1:  # Memegen's "top" layout: all lines stacked from the top
        boxes = [dict(boxes[0], anchor_y=0.2 * i, scale_y=0.2) for i in range(len(lines))]
    for line, box in zip(lines, boxes):
        if not line:
            continue
        text = line.upper() if box.get("style", "upper") == "upper" else line
        bw, bh = int(box.get("scale_x", 1.0) * width * 0.96), int(box.get("scale_y", 0.2) * height)
        layer = Image.new("RGBA", (max(1, bw), max(1, bh)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        font, wrapped = _fit(draw, text, bw, bh)
        size = font.size
        stroke = max(2, size // 14)
        total = len(wrapped) * size * 1.15
        y = (bh - total) / 2
        fill = box.get("color", "white")
        try:
            r, g, b = ImageColor.getrgb(fill)[:3]
        except ValueError:
            fill, (r, g, b) = "white", (255, 255, 255)
        outline = "black" if 0.299 * r + 0.587 * g + 0.114 * b > 128 else "white"  # dark text gets a light outline
        for part in wrapped:
            shown = visual_order(part)
            w = draw.textlength(shown, font=font)
            x = (bw - w) / 2 if box.get("align", "center") == "center" else (bw - w if box.get("align") == "right" else 0)
            draw.text((x, y), shown, font=font, fill=fill, stroke_width=stroke, stroke_fill=outline)
            y += size * 1.15
        angle = float(box.get("angle", 0) or 0)
        if angle:
            layer = layer.rotate(-angle, expand=True, resample=Image.BICUBIC)
        cx = box.get("anchor_x", 0.0) * width + box.get("scale_x", 1.0) * width / 2
        cy = box.get("anchor_y", 0.0) * height + box.get("scale_y", 0.2) * height / 2
        image.alpha_composite(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1("\n".join([template["id"], layout or "", *lines]).encode("utf-8")).hexdigest()[:16] + ".png"
    path = OUTPUT / name
    image.convert("RGB").save(path, "PNG", optimize=True)
    return path


# ---------------------------------------------------------------- tools
@mcp.tool(description="List meme templates. `filter` matches the name, id or keywords (e.g. 'drake', 'cat', 'success'). "
                      "Returns ids to use with generate_meme, the number of text lines each takes, and an example.")
@logged
def list_templates(filter: str = "", animated: bool = False, limit: int = 30) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        if filter or animated:
            params = {k: v for k, v in (("filter", filter.strip()[:100]), ("animated", "true" if animated else "")) if v}
            found = _get(client, "/templates/", **params) or []
        else:
            found = _templates(client)
    limit = max(1, min(int(limit), 300))
    return {"ok": True, "count": len(found), "templates": [_summary(t) for t in found[:limit]]}


@mcp.tool(description="Details of one meme template: name, how many text lines it takes, styles, keywords, example.")
@logged
def get_template(template_id: str) -> dict:
    template_id = _template_id(template_id)
    with httpx.Client(timeout=TIMEOUT) as client:
        template = _get(client, f"/templates/{template_id}")
    if not template:
        return {"ok": False, "error": f"Template {template_id!r} not found; use list_templates"}
    return {"ok": True, "template": _summary(template)}


@mcp.tool(description="Make a meme from a template and text lines (one per text box, in order: usually top, bottom). "
                      "Write normal text: spaces and punctuation are handled. Hebrew is supported (drawn locally). "
                      "extension: png, jpg, gif, webp. Optional font (thick, thin, comic, impact, notosans), style, "
                      "layout (default or top). Returns the image URL, or a local file for Hebrew memes.")
@logged
def generate_meme(template_id: str, text_lines: list[str], extension: str = "png", font: str = "",
                  style: str = "", layout: str = "") -> dict:
    template_id, lines = _template_id(template_id), _lines(text_lines)
    extension = _choice(extension, "extension", EXTENSIONS) or "png"
    font, style, layout = _choice(font, "font"), _choice(style, "style"), _choice(layout, "layout", LAYOUT_CHOICES)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        template = _get(client, f"/templates/{template_id}")
        if not template:
            return {"ok": False, "error": f"Template {template_id!r} not found; use list_templates"}
        result = {"ok": True, "template": template_id, "name": template["name"], "text": lines,
                  "expected_lines": template["lines"]}
        if any(HEBREW.search(line) for line in lines):
            path = render_locally(client, template, lines, layout)
            return {**result, "renderer": "local", "file": str(path), "url": None}
        query = {k: v for k, v in (("font", font), ("style", style), ("layout", layout)) if v}
        url = f"{BASE}/images/{template_id}/{'/'.join(_encode(line) for line in lines)}.{extension}"
        url += ("?" + urlencode(query)) if query else ""
        check = client.head(url)
        if check.status_code >= 400:
            return {"ok": False, "error": f"Memegen could not make this meme (HTTP {check.status_code})"}
    return {**result, "renderer": "memegen", "url": url}


@mcp.tool(description="Make a meme on your own background image (a public http(s) image URL) with text lines. "
                      "Local and private-network addresses are refused. Latin text only (Hebrew: use a template).")
@logged
def generate_custom_meme(background_url: str, text_lines: list[str], extension: str = "png", font: str = "") -> dict:
    background_url, lines = _public_url(background_url), _lines(text_lines)
    if any(HEBREW.search(line) for line in lines):
        return {"ok": False, "error": "Hebrew text works with templates (generate_meme); custom backgrounds are Latin-only"}
    extension = _choice(extension, "extension", EXTENSIONS) or "png"
    query = {"background": background_url, **({"font": f} if (f := _choice(font, "font")) else {})}
    url = f"{BASE}/images/custom/{'/'.join(_encode(line) for line in lines)}.{extension}?{urlencode(query)}"
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        check = client.head(url)
    if check.status_code >= 400:
        return {"ok": False, "error": f"Memegen could not use that background (HTTP {check.status_code})"}
    return {"ok": True, "template": "custom", "text": lines, "renderer": "memegen", "url": url}


@mcp.tool(description="Find example memes whose template matches a word (e.g. 'success', 'cat'): inspiration and formats.")
@logged
def search_meme_examples(query: str, limit: int = 10) -> dict:
    query = str(query or "").strip()[:100]
    if not query:
        return {"ok": False, "error": "query is empty"}
    with httpx.Client(timeout=TIMEOUT) as client:
        found = _get(client, "/images/", filter=query) or []
    limit = max(1, min(int(limit), 50))
    return {"ok": True, "count": len(found), "examples": [{"template": e["template"], "url": e["url"]} for e in found[:limit]]}


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
