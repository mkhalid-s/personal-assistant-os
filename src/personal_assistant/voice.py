"""Local voice I/O for the ``myos voice`` loop.

All three functions degrade gracefully: if the optional ``[voice]`` deps or a TTS
engine are missing they print a one-line hint and return empty rather than
crashing, so the same REPL still works in text.

Recording is toggle-to-talk (ENTER to start, ENTER to stop) -- portable across
terminals and headless hosts. Set ``MYOS_WHISPER_MODEL`` to pick a Whisper size
and ``MYOS_TTS_COMMAND`` to use a custom text-to-speech command (text on stdin).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import wave

_WHISPER = None  # cached model; loading is expensive, do it once per process


def record_push_to_talk(samplerate: int = 16000) -> str | None:
    """Record mic audio between two ENTER presses; return a temp WAV path."""
    try:
        import numpy as np
        import sounddevice as sd
    except Exception:
        print("Voice input needs the optional deps:  pip install -e '.[voice]'")
        return None

    frames: list = []

    def _callback(indata, _frames, _time, _status):
        frames.append(indata.copy())

    try:
        input("🎙  Press ENTER to start recording…")
        with sd.InputStream(samplerate=samplerate, channels=1, dtype="int16", callback=_callback):
            input("● recording — press ENTER to stop…")
    except Exception as exc:  # noqa: BLE001 - no mic / device error
        print(f"Could not access microphone: {exc}")
        return None

    if not frames:
        return None
    data = np.concatenate(frames, axis=0)
    # mkstemp (not the race-prone mktemp): atomically create the file and own the path.
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(data.tobytes())
    return path


def transcribe(wav_path: str) -> str:
    """In-process faster-whisper transcription (model cached across calls)."""
    global _WHISPER
    if not wav_path:
        return ""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        print("Transcription needs faster-whisper:  pip install -e '.[voice]'")
        return ""
    if _WHISPER is None:
        model_name = os.getenv("MYOS_WHISPER_MODEL", "base")
        _WHISPER = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = _WHISPER.transcribe(wav_path)
    return " ".join(seg.text.strip() for seg in segments if seg.text).strip()


def speak(text: str) -> None:
    """Best-effort text-to-speech: MYOS_TTS_COMMAND -> macOS `say` -> pyttsx3."""
    text = (text or "").strip()
    if not text:
        return
    command = os.getenv("MYOS_TTS_COMMAND", "").strip()
    if command:
        try:
            subprocess.run(shlex.split(command), input=text, text=True, capture_output=True, timeout=120, check=False)
            return
        except Exception:  # noqa: BLE001 - fall through to other engines
            pass
    if shutil.which("say"):
        subprocess.run(["say", text], check=False)
        return
    try:
        import pyttsx3

        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        return
    except Exception:  # noqa: BLE001 - no TTS engine; text is already on screen
        pass
