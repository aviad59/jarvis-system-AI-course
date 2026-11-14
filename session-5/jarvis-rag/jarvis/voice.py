"""Speech-to-text with OpenAI's Whisper model, running locally via faster-whisper.

faster-whisper runs the same open Whisper weights on CTranslate2: ~4x faster on CPU than
the reference implementation, no PyTorch and no ffmpeg (audio is decoded with PyAV).
Nothing leaves this machine and there is no API cost.

What this module adds on top of a plain model.transcribe() call:
  * level normalization, so a quiet or distant microphone is transcribed like a close one;
  * vocabulary biasing: titles and identifiers from the RAG index (service names, IDs such as
    SYS-001) are passed to Whisper as hotwords, so they come back spelled the way the notes spell them;
  * "Auto" language is limited to the languages Jarvis speaks (Hebrew, English), so Hebrew is
    no longer mistaken for Arabic or Yiddish;
  * known Whisper hallucinations on silence/noise ("Thanks for watching", subtitle credits) are dropped;
  * a confidence score, so the console can ask you to check a doubtful transcript before running it.

Settings (environment variables):
  JARVIS_WHISPER_MODEL      model for every language (default "small": ~460 MB, decent Hebrew on CPU).
                            "large-v3-turbo" is much more accurate and ~3x slower on CPU.
  JARVIS_WHISPER_MODEL_HE   optional model used only for Hebrew, e.g. "ivrit-ai/whisper-large-v3-turbo-ct2" (Whisper
                            fine-tuned on Hebrew by ivrit.ai, ~1.6 GB): the best local Hebrew, but ~10 s per
                            question on a CPU because the large encoder always processes a 30 s window; fast on a GPU.
  JARVIS_WHISPER_DEVICE     "auto" (default: CUDA GPU when one is usable, else CPU), "cpu" or "cuda".
  JARVIS_WHISPER_LANGUAGES  languages "Auto" may choose from (default "he,en").
  JARVIS_WHISPER_HOTWORDS   extra vocabulary, comma separated, added to the terms taken from the index.
"""
import io  # audio arrives as bytes; PyAV reads it straight from memory
import json  # vocabulary from the RAG index
import math  # confidence from log-probabilities
import os  # settings from the environment
import re  # vocabulary extraction, hallucination patterns
import threading  # load each model once, transcribe one clip at a time
import time  # timing
from pathlib import Path

MODEL_NAME = os.environ.get("JARVIS_WHISPER_MODEL", "small")
MODEL_HE = os.environ.get("JARVIS_WHISPER_MODEL_HE", "").strip() or None
DEVICE = os.environ.get("JARVIS_WHISPER_DEVICE", "auto").lower()
THREADS = int(os.environ.get("JARVIS_WHISPER_THREADS", os.cpu_count() or 4))
LANGUAGES = [code.strip() for code in os.environ.get("JARVIS_WHISPER_LANGUAGES", "he,en").split(",") if code.strip()]
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # ~25 MB, minutes of opus audio
SAMPLE_RATE = 16000  # what Whisper expects
TARGET_PEAK = 0.9  # quiet recordings are scaled up to this peak level
MAX_GAIN = 20.0  # ...but never by more than this, so pure noise isn't blown up into "speech"
REVIEW_BELOW = 0.55  # confidence under this: the console asks you to check the text instead of running it
MAX_HOTWORDS = 40

_models, _load_lock, _run_lock = {}, threading.Lock(), threading.Lock()
_vocabulary = {"key": None, "terms": []}

# Whisper was trained on subtitled video; on silence or noise it tends to "hear" these.
HALLUCINATIONS = re.compile(
    r"^(?:thank(?:s| you)(?: (?:so|very) much)?(?: for watching| for listening)?|thanks for watching|"
    r"please subscribe.*|subscribe.*|subtitles? by.*|transcri(?:bed|ption) by.*|amara\.org.*|"
    r"you|bye|\.+|♪+|תודה(?: רבה)?(?: שצפיתם| על הצפייה| שהאזנתם)?|תודה שצפיתם|כתוביות.*|תרגום.*|הירשמו.*|שלום)$",
    re.IGNORECASE)
ALWAYS_HALLUCINATION = re.compile(r"subtitles? by|amara\.org|transcri(?:bed|ption) by|כתוביות|הירשמו לערוץ", re.IGNORECASE)


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def device():  # (device, compute type); a CUDA GPU makes even the large models fast
    if DEVICE in ("cpu", "cuda"):
        chosen = DEVICE
    else:
        try:
            import ctranslate2
            chosen = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            chosen = "cpu"
    return chosen, ("float16" if chosen == "cuda" else "int8")  # int8: smaller and faster on CPU


def model_for(language):
    return MODEL_HE if language == "he" and MODEL_HE else MODEL_NAME


def status():
    if not available():
        import sys
        return {"ok": False, "label": "Whisper (voice)",
                "detail": f"faster-whisper missing in {sys.executable} — run it there with -m pip install faster-whisper"}
    names = [MODEL_NAME] + ([f"{MODEL_HE} (Hebrew)"] if MODEL_HE else [])
    state = "loaded" if _models else "loads on first use"
    return {"ok": True, "label": "Whisper (voice)", "detail": f"model {', '.join(names)}, local {device()[0].upper()} · {state}"}


def model(name=None):
    name = name or MODEL_NAME
    with _load_lock:
        if name not in _models:
            from faster_whisper import WhisperModel
            where, compute = device()
            try:
                _models[name] = WhisperModel(name, device=where, compute_type=compute, cpu_threads=THREADS)
            except Exception:
                if where == "cpu":
                    raise
                _models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=THREADS)  # CUDA libraries missing
        return _models[name]


def decode(audio):  # any browser format (webm/ogg/wav/m4a) -> 16 kHz mono float32, the input Whisper expects
    # Decoding here instead of in faster-whisper avoids its PyAV version coupling
    # (faster-whisper 1.2 passes an av.open() option that newer PyAV releases removed).
    import av
    import numpy as np
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    with av.open(io.BytesIO(audio)) as container:  # from memory: no temp file
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray() for out in resampler.resample(frame))
        chunks.extend(out.to_ndarray() for out in resampler.resample(None))  # flush the resampler
    if not chunks:
        raise ValueError("The recording contains no audio")
    return np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32) / 32768.0


def normalize(samples):  # remove DC offset and bring quiet speech up to a normal level
    import numpy as np
    samples = samples - float(samples.mean())
    peak = float(np.percentile(np.abs(samples), 99.9)) if samples.size else 0.0  # ignore single clicks
    if peak < 1e-4:
        return samples  # digital silence
    gain = min(MAX_GAIN, TARGET_PEAK / peak)
    return np.clip(samples * gain, -1.0, 1.0) if gain > 1.0 else samples


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")


def identifiers(text):
    """Names worth spelling right: SYS-001, p95, HTTP, and capitalized names (Cedar) that aren't just sentence starts."""
    for match in _WORD.finditer(text):
        word, before = match.group(), text[:match.start()].rstrip()
        if len(word) < 2 or (match.start() and text[match.start() - 1].isalnum()):
            continue
        if any(c.isdigit() for c in word) or "-" in word or "_" in word:
            yield word
        elif word.isupper() and len(word) <= 8:
            yield word
        elif word[0].isupper() and word[1:].islower() and before and before[-1] not in ".!?:#*>-\n":
            yield word


def vocabulary():
    """Terms Whisper should spell the way the notes do: document titles and identifiers from the RAG index."""
    try:
        import rag
        path = rag.INDEX_FILE
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except Exception:
        path, key = None, None
    if key != _vocabulary["key"]:
        terms = {}
        if path:
            try:
                records = json.loads(Path(path).read_text(encoding="utf-8")).get("records", [])
            except Exception:
                records = []
            for record in records:
                meta = record.get("metadata", {})
                for title in (meta.get("title"), meta.get("document_id")):
                    if title and len(title) <= 40:
                        terms[title] = terms.get(title, 0) + 5
                for word in identifiers(record.get("text", "")):
                    terms[word] = terms.get(word, 0) + 1
        ranked = sorted(terms, key=lambda t: -terms[t])[:MAX_HOTWORDS]
        _vocabulary.update(key=key, terms=ranked)
    extra = [t.strip() for t in os.environ.get("JARVIS_WHISPER_HOTWORDS", "").split(",") if t.strip()]
    return extra + [t for t in _vocabulary["terms"] if t not in extra]


def pick_language(samples, language):
    """The requested language, or the most likely of LANGUAGES ('Auto')."""
    if language:
        return language, 1.0
    if not LANGUAGES:
        return None, 0.0
    _, _, probabilities = model().detect_language(samples, vad_filter=True)
    scores = {code: p for code, p in probabilities if code in LANGUAGES}
    if not scores:
        return LANGUAGES[0], 0.0
    best = max(scores, key=scores.get)
    return best, scores[best] / (sum(scores.values()) or 1)


def clean(segments):
    """Keep real speech: drop segments Whisper itself marks as non-speech, and known hallucinations."""
    kept = []
    for segment in segments:
        text = re.sub(r"\s+", " ", segment.text).strip()
        if not text:
            continue
        if segment.no_speech_prob > 0.6 and segment.avg_logprob < -1.0:  # Whisper's own "this was silence" rule
            continue
        if ALWAYS_HALLUCINATION.search(text):
            continue
        if HALLUCINATIONS.match(text.strip(" .!?,")) and (segment.no_speech_prob > 0.2 or segment.avg_logprob < -0.7):
            continue
        kept.append((segment, text))
    return kept


def transcribe(audio, language=None, suffix=None):
    """Transcribe audio bytes; language is an ISO code ('he', 'en') or None to auto-detect.

    suffix is accepted for compatibility; the container format is detected from the bytes.
    """
    if not audio:
        raise ValueError("No audio received")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("Audio clip is too long")
    started = time.time()
    try:
        samples = decode(audio)
    except ValueError:
        raise
    except Exception as error:  # corrupt or unsupported container
        raise ValueError(f"Could not decode the audio ({type(error).__name__})") from None
    samples = normalize(samples)
    audio_seconds = round(len(samples) / SAMPLE_RATE, 2)
    with _run_lock:  # one clip at a time; the CPU is the bottleneck anyway
        language, language_probability = pick_language(samples, language or None)
        name = model_for(language)
        hotwords = ", ".join(vocabulary()) or None
        segments, info = model(name).transcribe(
            samples, language=language, beam_size=5,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
            condition_on_previous_text=False,  # short questions: avoids the repetition loops long context can cause
            hotwords=hotwords)
        kept = clean(segments)  # segments is lazy: decoding happens here
    text = " ".join(text for _, text in kept).strip()
    spoken = sum(max(0.01, s.end - s.start) for s, _ in kept)
    confidence = math.exp(sum(s.avg_logprob * max(0.01, s.end - s.start) for s, _ in kept) / spoken) if kept else 0.0
    return {"text": text, "language": info.language,
            "language_probability": round(language_probability if language_probability < 1 else info.language_probability, 3),
            "confidence": round(confidence, 3), "review": bool(text) and confidence < REVIEW_BELOW,
            "audio_seconds": audio_seconds, "seconds": round(time.time() - started, 2), "model": name}
