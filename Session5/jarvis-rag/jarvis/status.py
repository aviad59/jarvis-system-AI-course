"""Health checks for the systems the pipeline depends on.

Each check returns {'ok': bool, 'label', 'detail', ...extra}. Secrets (URIs, keys) are
never included - only whether they exist and where they came from.
"""
import json  # read the local index and Ollama's reply
import re  # strip cluster hostnames from Atlas errors
import sys  # import exercise 1's Atlas helpers
import threading  # cache guard
import time  # cache age
import urllib.request  # talk to Ollama
from concurrent.futures import ThreadPoolExecutor  # run checks in parallel
import rag  # paths and the Anthropic key lookup

OLLAMA = "http://127.0.0.1:11434"
REQUIRED_MODELS = ("embeddinggemma", "gemma3:4b")
CACHE_SECONDS = 20
_cache, _lock = {"time": 0, "value": None}, threading.Lock()


def check_ollama():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as response:
            models = [m["name"] for m in json.loads(response.read())["models"]]
    except Exception:
        return {"ok": False, "label": "Ollama", "detail": "not running on 127.0.0.1:11434 (start the Ollama app)"}
    have = {name: any(m == name or m.startswith(name + ":") for m in models) for name in REQUIRED_MODELS}
    missing = [name for name, ok in have.items() if not ok]
    return {"ok": not missing, "label": "Ollama", "models": models,
            "detail": f"{len(models)} models" + (f"; missing {', '.join(missing)}" if missing else "")}


def atlas_error_detail(error, settings, safe_error):  # one short line, without cluster hostnames
    if type(error).__name__ == "ServerSelectionTimeoutError":  # DNS, TLS or IP allowlist problems all end up here
        return "cannot reach the cluster (check Atlas Network Access / your IP); Vectors from: local works without it"
    message = re.sub(r"[\w.-]+\.mongodb\.net(:\d+)?", "<cluster>", safe_error(error, settings))
    return message if len(message) <= 160 else message[:157] + "..."


def check_atlas():
    if str(rag.EX1) not in sys.path:
        sys.path.append(str(rag.EX1))
    try:
        import atlas_upload
        settings, problem = atlas_upload.atlas_settings(rag.EX1)
    except Exception as error:
        return {"ok": False, "label": "MongoDB Atlas", "detail": f"{type(error).__name__}: {error}"}
    if problem:
        return {"ok": False, "label": "MongoDB Atlas", "detail": problem}
    try:
        client = atlas_upload.connect(settings["uri"])
        try:
            db = client[settings["db"]]
            count = db[settings["collection"]].count_documents({})
            manifest = db["index_manifests"].find_one({"_id": settings["collection"]},
                                                      {"_id": 0, "model": 1, "dimensions": 1, "chunk_count": 1}) or {}
        finally:
            client.close()
    except Exception as error:
        return {"ok": False, "label": "MongoDB Atlas", "detail": atlas_error_detail(error, settings, atlas_upload.safe_error)}
    return {"ok": count > 0, "label": "MongoDB Atlas", "db": settings["db"], "collection": settings["collection"],
            "manifest": manifest, "detail": f"{count} chunks in {settings['db']}.{settings['collection']}"
            + (f" · {manifest.get('model')} {manifest.get('dimensions')}d" if manifest else "")}


def check_anthropic():
    key, source = rag.find_anthropic_key()
    return {"ok": bool(key), "label": "Anthropic API",
            "detail": f"key found ({source})" if key else "ANTHROPIC_API_KEY not set — use the ollama provider or add a .env"}


def check_index():
    path = rag.EX1 / "build" / "index.json"
    if not path.exists():
        return {"ok": False, "label": "Local index", "detail": "not built yet (run with indexing enabled)"}
    manifest = json.loads(path.read_text(encoding="utf-8"))["manifest"]
    updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    return {"ok": True, "label": "Local index",
            "detail": f"{manifest['chunk_count']} chunks · {manifest.get('corpus', 'course')} · built {updated}"}


def check_vault():
    try:
        files, scope = rag._vault_support().vault_files(rag.VAULT)
    except ValueError as error:
        return {"ok": False, "label": "Obsidian vault", "detail": f"{error} (set JARVIS_VAULT)"}
    import vault_watch
    return {"ok": bool(files), "label": "Obsidian vault",
            "detail": f"{len(files)} notes · {scope} · {vault_watch.state()}"}


def check_voice():
    import voice  # local import: keeps server start fast and the check honest
    return voice.status()


def check_tts():
    import tts
    return tts.status()


CHECKS = {"ollama": check_ollama, "atlas": check_atlas, "anthropic": check_anthropic, "vault": check_vault, "index": check_index,
          "voice": check_voice, "tts": check_tts}


def check_all(force=False):  # all checks in parallel, cached briefly (Atlas takes ~1-2 s)
    with _lock:
        if not force and _cache["value"] and time.time() - _cache["time"] < CACHE_SECONDS:
            return _cache["value"]
    with ThreadPoolExecutor(len(CHECKS)) as pool:
        futures = {name: pool.submit(check) for name, check in CHECKS.items()}
        result = {}
        for name, future in futures.items():
            try:
                result[name] = future.result(timeout=30)
            except Exception as error:
                result[name] = {"ok": False, "label": name, "detail": f"check failed: {type(error).__name__}"}
    result = {"checked": time.time(), "systems": result}
    with _lock:
        _cache.update(time=time.time(), value=result)
    return result
