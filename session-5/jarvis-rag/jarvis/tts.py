"""Text-to-speech with Meta's MMS-TTS (VITS) models, running locally via Hugging Face transformers.

MMS-TTS has a Hebrew voice (facebook/mms-tts-heb, ~145 MB), which most browsers and
Windows installs lack. Nothing leaves this machine and there is no API cost.
Weights are downloaded once from Hugging Face on first use, then cached.

Models per language are set with JARVIS_TTS_MODEL_HE / JARVIS_TTS_MODEL_EN.
Note: the MMS weights are licensed CC-BY-NC 4.0 (non-commercial use).
"""
import io  # build the WAV in memory
import os  # model selection from the environment
import re  # sentence splitting and text cleanup
import threading  # load each model once, synthesize one clip at a time
import time  # timing
import wave  # WAV container, standard library

MODELS = {"he": os.environ.get("JARVIS_TTS_MODEL_HE", "facebook/mms-tts-heb"),
          "en": os.environ.get("JARVIS_TTS_MODEL_EN", "facebook/mms-tts-eng")}
MAX_TEXT_CHARS = 2000
CHUNK_CHARS = 180  # VITS quality drops on very long inputs; synthesize sentence by sentence
PAUSE_SECONDS = 0.22  # silence between sentences
SPEAKING_RATE = float(os.environ.get("JARVIS_TTS_RATE", "1.0"))  # >1 faster, <1 slower
_models, _load_lock, _run_lock = {}, threading.Lock(), threading.Lock()
# Physical cores, not hyperthreads: more threads than cores made synthesis slower and starved other work.
THREADS = int(os.environ.get("JARVIS_TTS_THREADS", max(1, (os.cpu_count() or 2) // 2)))
CACHE_SIZE = 64  # recent clips; replaying an answer is then instant
_cache, _cache_lock = {}, threading.Lock()
HEBREW = re.compile(r"[֐-׿]")


def available():
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except ImportError:
        return False


def status():
    if not available():
        import sys
        return {"ok": False, "label": "MMS-TTS (speech)",
                "detail": f"torch/transformers missing in {sys.executable} — run it there with -m pip install torch transformers"}
    loaded = [lang for lang in MODELS if lang in _models]
    state = f"loaded: {', '.join(loaded)}" if loaded else "loads on first use"
    return {"ok": True, "label": "MMS-TTS (speech)", "detail": f"{MODELS['he']}, local CPU · {state}"}


def load(language):
    with _load_lock:
        if language not in _models:
            import torch
            from transformers import AutoTokenizer, VitsModel
            torch.set_num_threads(THREADS)
            name = MODELS[language]
            try:  # cached weights first: no Hugging Face check, so it is faster and works offline
                model, tokenizer = VitsModel.from_pretrained(name, local_files_only=True), AutoTokenizer.from_pretrained(name, local_files_only=True)
            except OSError:  # not downloaded yet
                model, tokenizer = VitsModel.from_pretrained(name), AutoTokenizer.from_pretrained(name)
            model = model.eval()
            model.speaking_rate = SPEAKING_RATE
            _models[language] = (tokenizer, model)
        return _models[language]


# -- text preparation -----------------------------------------------------------
# MMS tokenizers drop characters outside their alphabet, so digits would be silently skipped.
# Numbers are spelled out in Hebrew (feminine counting form, as when reading a number aloud).
_ONES = ["אפס", "אחת", "שתיים", "שלוש", "ארבע", "חמש", "שש", "שבע", "שמונה", "תשע"]
_TEENS = ["עשר", "אחת עשרה", "שתים עשרה", "שלוש עשרה", "ארבע עשרה", "חמש עשרה", "שש עשרה", "שבע עשרה",
          "שמונה עשרה", "תשע עשרה"]
_TENS = ["", "", "עשרים", "שלושים", "ארבעים", "חמישים", "שישים", "שבעים", "שמונים", "תשעים"]
_HUNDREDS = ["", "מאה", "מאתיים", "שלוש מאות", "ארבע מאות", "חמש מאות", "שש מאות", "שבע מאות", "שמונה מאות", "תשע מאות"]
_THOUSANDS = ["", "אלף", "אלפיים", "שלושת אלפים", "ארבעת אלפים", "חמשת אלפים", "ששת אלפים", "שבעת אלפים",
              "שמונת אלפים", "תשעת אלפים"]


def _atomic(n):  # one number word (or a teen); "and" goes before it: מאה ועשרים, but מאה עשרים ושלוש
    return n < 20 or (n < 100 and n % 10 == 0) or (n < 1000 and n % 100 == 0)


def _with_rest(head, rest):
    if not rest:
        return head
    words = hebrew_number(rest)
    return f"{head} {'ו' + words if _atomic(rest) else words}"


def hebrew_number(n):
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        return _with_rest(_TENS[n // 10], n % 10)
    if n < 1000:
        return _with_rest(_HUNDREDS[n // 100], n % 100)
    if n < 10000:
        return _with_rest(_THOUSANDS[n // 1000], n % 1000)
    return " ".join(_ONES[int(d)] for d in str(n))  # long numbers (IDs, phones): digit by digit


def prepare(text, language):
    text = re.sub(r"\[[^\]]*\]", " ", text)  # citation markers like [chunk-3]
    text = re.sub(r"https?://\S+", " ", text)
    if language == "he":
        text = re.sub(r"(\d+)%", lambda m: m.group(1) + " אחוז", text)
        text = re.sub(r"\d+", lambda m: " " + hebrew_number(int(m.group())) + " ", text)
        text = re.sub(r"[֑-ׇ]", "", text)  # niqqud and cantillation: the model is trained without them
    return re.sub(r"\s+", " ", text).strip()


def chunks(text):  # sentences, merged up to CHUNK_CHARS; overly long sentences split on commas/spaces
    pieces = []
    for sentence in re.split(r"(?<=[.!?;:\n])\s+", text):
        while len(sentence) > CHUNK_CHARS:
            cut = max(sentence.rfind(",", 0, CHUNK_CHARS), sentence.rfind(" ", 0, CHUNK_CHARS))
            cut = cut if cut > 0 else CHUNK_CHARS
            pieces.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:]
        if sentence.strip():
            pieces.append(sentence.strip())
    merged = []
    for piece in pieces:
        if merged and len(merged[-1]) + len(piece) + 1 <= CHUNK_CHARS:
            merged[-1] += " " + piece
        else:
            merged.append(piece)
    return merged


# -- English inside Hebrew --------------------------------------------------------
# The Hebrew voice can't pronounce Latin letters, so mixed text is split into Hebrew and English runs and each
# run is spoken by its own model. Neither model reads digits or symbols, so English runs are normalized too.
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9_'’.\-/]*(?:[ \t]+[A-Za-z][A-Za-z0-9_'’.\-/]*)*")
_EN_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
            "sixteen seventeen eighteen nineteen").split()
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_LETTER_NAMES = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", ("ay bee see dee ee eff jee aitch eye jay kay el em en oh pee "
                                                       "cue ar ess tee you vee double-you ex why zee").split()))


def english_number(n):
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        return _EN_TENS[n // 10] + ("" if n % 10 == 0 else " " + _EN_ONES[n % 10])
    if n < 1000:
        return _EN_ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + english_number(n % 100))
    if n < 10000:
        return english_number(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + english_number(n % 1000))
    return " ".join(_EN_ONES[int(d)] for d in str(n))  # long numbers (IDs): digit by digit


def prepare_english(text):
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(\d+)%", r"\1 percent", text)
    text = re.sub(r"[_/\-]+", " ", text)  # notification_delay_p95 -> notification delay p95
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)  # p95 -> p 95
    text = re.sub(r"\b[A-Z]{2,5}\b", lambda m: " ".join(_LETTER_NAMES[c] for c in m.group()), text)  # HTTP -> aitch tee tee pee
    text = re.sub(r"\d+", lambda m: " " + (" ".join(_EN_ONES[int(d)] for d in m.group()) if m.group().startswith("0") and len(m.group()) > 1
                                           else english_number(int(m.group()))) + " ", text)  # 001 -> zero zero one
    return re.sub(r"\s+", " ", text).strip(" .,;:")


def segments(text, language):
    """[(language, text), ...] ready for synthesis; Latin runs inside Hebrew go to the English voice."""
    if language == "en":
        return [("en", prepare_english(text))]
    out, position = [], 0
    for match in LATIN_RUN.finditer(text):
        out.append(("he", prepare(text[position:match.start()], "he")))
        out.append(("en", prepare_english(match.group())))
        position = match.end()
    out.append(("he", prepare(text[position:], "he")))
    return [(lang, part) for lang, part in out if re.search(r"[A-Za-z֐-׿]", part)]  # drop runs with nothing speakable


def detect_language(text):
    return "he" if HEBREW.search(text) else "en"


def warm_up():  # load both voices and run them once, so the first real reply doesn't pay the loading cost
    if available():
        for language, sample in (("he", "שלום"), ("en", "hello")):
            synthesize(sample, language)


def synthesize(text, language=None):
    """Return (wav_bytes, info), from the cache when the same text was spoken recently."""
    key = (language, str(text or "").strip())
    with _cache_lock:
        if key in _cache:
            _cache[key] = _cache.pop(key)  # mark as most recently used
            wav, info = _cache[key]
            return wav, {**info, "seconds": 0.0, "cached": True}
    wav, info = _synthesize(text, language)
    with _cache_lock:
        _cache[key] = (wav, info)
        while len(_cache) > CACHE_SIZE:
            _cache.pop(next(iter(_cache)))  # drop the oldest
    return wav, info


def _synthesize(text, language=None):
    """Return (wav_bytes, info). language is 'he', 'en' or None to detect from the text."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("No text to speak")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    language = language or detect_language(text)
    if language not in MODELS:
        raise ValueError(f"Unsupported language '{language}' (supported: {', '.join(MODELS)})")
    cleaned = re.sub(r"https?://\S+", " ", re.sub(r"\[[^\]]*\]", " ", text))  # citations and links are never spoken
    parts = [segments(chunk, language) for chunk in chunks(cleaned)]
    if not any(parts):
        raise ValueError("Nothing speakable in the text")
    started = time.time()
    import numpy as np
    import torch
    needed = {lang for part in parts for lang, _ in part}
    voices = {lang: load(lang) for lang in needed}  # both voices output 16 kHz, so their clips can be joined directly
    rate = voices[next(iter(needed))][1].config.sampling_rate
    pause = np.zeros(int(rate * PAUSE_SECONDS), dtype=np.float32)
    joint = np.zeros(int(rate * 0.06), dtype=np.float32)  # a tiny gap where the voice switches language
    audio = []
    with _run_lock, torch.inference_mode():
        for part in parts:
            for lang, piece in part:
                tokenizer, model = voices[lang]
                inputs = tokenizer(piece, return_tensors="pt")
                if inputs["input_ids"].shape[-1] == 0:  # nothing this model can pronounce
                    continue
                torch.manual_seed(0)  # VITS samples noise: a fixed seed gives the same voice every time
                audio += [model(**inputs).waveform[0].numpy(), joint]
            audio.append(pause)
    if not any(len(a) for a in audio if a is not pause and a is not joint):
        raise ValueError("Nothing speakable in the text")
    samples = np.concatenate(audio)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    info = {"language": language, "model": "+".join(MODELS[lang] for lang in sorted(needed)), "audio_seconds": round(len(samples) / rate, 2),
            "seconds": round(time.time() - started, 2)}
    return buffer.getvalue(), info


# -- neural voices, streamed (default) ----------------------------------------------
# Microsoft's neural voices through edge-tts: natural Hebrew (reads unvoweled text, numbers and English terms,
# which MMS can't) and English, with the first audio in under a second. Needs internet: the spoken text is sent
# to Microsoft's speech service. Set JARVIS_TTS_ENGINE=local to keep speech fully on this computer.
# If the service can't be reached, Jarvis falls back to the local MMS voices automatically.
ENGINE = os.environ.get("JARVIS_TTS_ENGINE", "edge").strip().lower()  # edge | local
VOICES = {"he": os.environ.get("JARVIS_TTS_VOICE_HE", "he-IL-AvriNeural"),
          "en": os.environ.get("JARVIS_TTS_VOICE_EN", "en-US-GuyNeural")}
EDGE_RATE = f"{round((SPEAKING_RATE - 1) * 100):+d}%"
EDGE_TIMEOUT = 5  # seconds to the first audio before falling back to the local voice
EDGE_RETRY_AFTER = 60  # after a failure (offline), use the local voice for this long before trying again
_edge = {"failed_at": 0.0, "error": None}


def edge_installed():
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


def edge_ready():  # use the neural voices now?
    return ENGINE == "edge" and edge_installed() and time.time() - _edge["failed_at"] > EDGE_RETRY_AFTER


def prepare_neural(text):  # the neural voices read numbers, % and English themselves; only remove what must not be spoken
    text = re.sub(r"\[[^\]]*\]", " ", str(text or ""))  # citation markers like [chunk-3]
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>|~]+", " ", text)  # Markdown symbols
    text = re.sub(r"(?<=[֐-׿])[-־](?=[A-Za-z])", " ", text)  # ב-Auth -> ב Auth: the English word is read as a word
    return re.sub(r"\s+", " ", text).strip()[:MAX_TEXT_CHARS]


def edge_stream(text, language=None):
    """Yield MP3 bytes as they arrive. Raises ConnectionError before the first chunk if the service fails."""
    import queue
    text = prepare_neural(text)
    if not re.search(r"\w", text):
        raise ValueError("Nothing speakable in the text")
    language = language or detect_language(text)
    if language not in VOICES:
        raise ValueError(f"Unsupported language '{language}' (supported: {', '.join(VOICES)})")
    key = ("edge", language, text)
    with _cache_lock:
        cached = _cache.pop(key, None)
        if cached:
            _cache[key] = cached  # most recently used
    if cached:
        yield cached[0]
        return
    chunks_out, done = queue.Queue(), object()

    def worker():  # edge-tts is async: run it on its own event loop and hand the bytes over through a queue
        import asyncio
        import edge_tts

        async def produce():
            async for chunk in edge_tts.Communicate(text, VOICES[language], rate=EDGE_RATE).stream():
                if chunk["type"] == "audio" and chunk["data"]:
                    chunks_out.put(chunk["data"])
        try:
            asyncio.run(produce())
            chunks_out.put(done)
        except Exception as error:
            chunks_out.put(error)

    threading.Thread(target=worker, daemon=True, name="edge-tts").start()
    received = []
    while True:
        try:
            item = chunks_out.get(timeout=30 if received else EDGE_TIMEOUT)
        except queue.Empty:
            item = TimeoutError("no audio from the speech service")
        if item is done:
            break
        if isinstance(item, Exception):
            if not received:  # nothing sent yet: the caller uses the local voice
                _edge.update(failed_at=time.time(), error=type(item).__name__)
                raise ConnectionError(f"Neural voice unavailable ({type(item).__name__})")
            return  # failed mid-stream: end the clip here (not cached)
        received.append(item)
        yield item
    _edge.update(error=None)
    with _cache_lock:
        _cache[key] = (b"".join(received), {"language": language, "model": VOICES[language], "engine": "edge"})
        while len(_cache) > CACHE_SIZE:
            _cache.pop(next(iter(_cache)))


def speech_stream(text, language=None):
    """(content_type, iterator of bytes, info): the neural voice streamed as MP3, else the local MMS voice as one WAV."""
    if edge_ready():
        stream = edge_stream(text, language)
        try:
            first = next(stream)  # a connection failure surfaces here, before any byte goes to the browser
        except ConnectionError:
            stream = None  # fall back to the local voice
        except StopIteration:
            raise ValueError("Nothing speakable in the text") from None
        if stream is not None:
            def chained():
                yield first
                yield from stream
            lang = language or detect_language(prepare_neural(text))
            return "audio/mpeg", chained(), {"engine": "edge", "model": VOICES.get(lang, ""), "language": lang}
    wav, info = synthesize(text, language)
    return "audio/wav", iter([wav]), {**info, "engine": "local"}


def warm_up_speech():  # at server start: load edge-tts and open one connection; if that fails, load the local voices now
    if edge_ready():
        try:
            for _ in edge_stream("שלום", "he"):
                pass
            return "neural voice (online)"
        except ConnectionError:
            pass  # offline: edge_ready() is now False for a minute, so replies go straight to MMS
    warm_up()
    return "MMS-TTS (local)" if available() else "no voice (install edge-tts or torch + transformers)"


def engine_status():
    if ENGINE == "edge" and edge_installed():
        if edge_ready():
            return {"ok": True, "label": "Speech (TTS)", "engine": "edge",
                    "detail": f"neural, streamed: {VOICES['he']} / {VOICES['en']} · local MMS fallback"}
        return {"ok": True, "label": "Speech (TTS)", "engine": "local",
                "detail": f"neural voice unreachable ({_edge['error']}); local MMS for now, retrying in a minute"}
    local = status()
    hint = "" if ENGINE == "local" else " · pip install edge-tts for natural voices"
    return {**local, "label": "Speech (TTS)", "engine": "local", "detail": "local MMS: " + local["detail"] + hint}
