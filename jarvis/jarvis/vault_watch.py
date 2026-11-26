"""Vault watcher: keeps the index in step with the Obsidian vault while the server runs.

Every POLL seconds it compares the (path, modified time, size) of the notes in scope. After a
change it waits until the vault has been quiet for SETTLE seconds (Obsidian saves while you
type), checks that the index really differs from the notes (content hashes), and starts the
vault_sync skill. Only new or changed chunks are embedded; deleted notes drop out of the index.

The sync runs in the background: questions asked meanwhile answer from the last finished index.
If another run is going, it tries again on the next poll. If a sync fails (for example Ollama
is closed), it waits for the next change in the vault; asking a question also re-indexes first.
"""
import threading  # background polling
import time  # poll interval and settle time

import rag  # vault path, index state
import skills  # LAST_INDEX: which documents / vector store the last question used
from runs import Busy  # another run is active

POLL, SETTLE = 2.0, 2.0
_state = {"text": "starting", "thread": None}


def state():  # one short line for the Systems panel
    run = _state.get("run")
    if run is not None and run.status == "running":  # e.g. "re-indexing in the background · embedded 216/2209"
        progress = next((e["line"].split(":", 1)[1].strip() for e in reversed(run.events)
                         if e["type"] == "log" and e["line"].startswith("EMBEDDED:")), None)
        return "re-indexing in the background" + (f" · embedded {progress}" if progress else "")
    return _state["text"]


def _log(text):
    _state["text"] = text
    print(f"VAULT WATCH: {text}", flush=True)


def _loop(manager):
    support = rag._vault_support()
    last_snapshot, changed_at, pending = None, 0.0, True  # pending at start: catch edits made while the server was off
    failed_snapshot, run = None, None
    while True:
        time.sleep(POLL)
        try:
            if run is not None and run.status != "running":  # the sync we started has finished
                if run.status == "error":
                    failed_snapshot = run_snapshot
                    _log("sync failed (see Mission log); will retry after the next change")
                else:
                    _state["text"] = f"live · synced {time.strftime('%H:%M:%S')}"
                run = None
            target = dict(skills.LAST_INDEX)
            if target["documents"] == "course":
                _state["text"] = "paused (Documents: course)"
                continue
            snapshot = support.vault_snapshot(rag.VAULT)
            if snapshot != last_snapshot:
                if last_snapshot is not None:
                    _state["text"] = "change detected…"
                last_snapshot, changed_at, pending = snapshot, time.time(), True
                continue
            if not pending or time.time() - changed_at < SETTLE or run is not None:
                continue
            if snapshot == failed_snapshot:  # same vault content that just failed: don't loop on the error
                pending = False
                continue
            if not rag.index_is_stale(target["documents"]):
                pending = False
                if not _state["text"].startswith("live"):
                    _state["text"] = "live · index up to date"
                continue
            try:
                run = manager.start("vault_sync", target)
            except Busy:
                _state["text"] = "change waiting for the current run"
                continue
            run_snapshot, pending, _state["run"] = snapshot, False, run
            _log(f"notes changed -> re-indexing (run {run.id})")
        except Exception as error:  # never let the watcher die; report and keep polling
            _log(f"error: {type(error).__name__}: {error}")


def start(manager):
    if _state["thread"] is None:
        _state["thread"] = threading.Thread(target=_loop, args=(manager,), daemon=True, name="vault-watch")
        _state["thread"].start()
        print(f"VAULT WATCH: watching {rag.VAULT}", flush=True)
