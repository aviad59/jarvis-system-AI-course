"""Speech-to-text with OpenAI's Whisper model, running locally via faster-whisper.

faster-whisper runs the same open Whisper weights on CTranslate2: ~4x faster on CPU than
the reference implementation, no PyTorch and no ffmpeg (audio is decoded with PyAV).
Nothing leaves this machine and there is no API cost.

Model size is set with JARVIS_WHISPER_MODEL (default "small": ~460 MB, decent Hebrew on CPU).
Larger models ("medium", "large-v3-turbo") are more accurate and much slower on CPU.
"""
import os  # model selection from the environment
import tempfile  # audio arrives as bytes; faster-whisper reads a file
import threading  # load the model once, transcribe one clip at a time
import time  # timing

MODEL_NAME = os.environ.get("JARVIS_WHISPER_MODEL", "small")
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # ~25 MB, minutes of opus audio
_model, _load_lock, _run_lock = None, threading.Lock(), threading.Lock()


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def status():
    if not available():
        import sys
        return {"ok": False, "label": "Whisper (voice)",
                "detail": f"faster-whisper missing in {sys.executable} — run it there with -m pip install faster-whisper"}
    state = "loaded" if _model else "loads on first use"
    return {"ok": True, "label": "Whisper (voice)", "detail": f"model {MODEL_NAME}, local CPU · {state}"}


def model():
    global _model
    with _load_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")  # int8: smaller and faster on CPU
        return _model


def decode(path):  # any browser format (webm/ogg/wav/m4a) -> 16 kHz mono float32, the input Whisper expects
    # Decoding here instead of in faster-whisper avoids its PyAV version coupling
    # (faster-whisper 1.2 passes an av.open() option that newer PyAV releases removed).
    import av
    import numpy as np
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray() for out in resampler.resample(frame))
        chunks.extend(out.to_ndarray() for out in resampler.resample(None))  # flush the resampler
    if not chunks:
        raise ValueError("The recording contains no audio")
    return np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32) / 32768.0


def transcribe(audio, language=None, suffix=".webm"):
    """Transcribe audio bytes; language is an ISO code ('he', 'en') or None to auto-detect."""
    if not audio:
        raise ValueError("No audio received")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("Audio clip is too long")
    started = time.time()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as file:  # delete=False: Windows can't reopen an open temp file
        file.write(audio)
        path = file.name
    try:
        try:
            samples = decode(path)
        except ValueError:
            raise
        except Exception as error:  # corrupt or unsupported container
            raise ValueError(f"Could not decode the audio ({type(error).__name__})") from None
        with _run_lock:  # one clip at a time; the CPU is the bottleneck anyway
            segments, info = model().transcribe(samples, language=language or None, beam_size=5, vad_filter=True)
            text = " ".join(segment.text.strip() for segment in segments).strip()  # segments is lazy: decoding happens here
    finally:
        os.unlink(path)
    return {"text": text, "language": info.language, "language_probability": round(info.language_probability, 3),
            "audio_seconds": round(info.duration, 2), "seconds": round(time.time() - started, 2), "model": MODEL_NAME}
