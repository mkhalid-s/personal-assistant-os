from __future__ import annotations

import subprocess


def transcribe_audio(audio_path: str, manual_text: str = "") -> str:
    if manual_text.strip():
        return manual_text.strip()

    py = (
        "import sys\n"
        "audio = sys.argv[1]\n"
        "try:\n"
        "  from faster_whisper import WhisperModel\n"
        "except Exception:\n"
        "  print('')\n"
        "  raise SystemExit(0)\n"
        "model = WhisperModel('base', device='cpu', compute_type='int8')\n"
        "segments, _ = model.transcribe(audio)\n"
        "print(' '.join(seg.text.strip() for seg in segments if seg.text).strip())\n"
    )
    out = subprocess.run(["python3", "-c", py, audio_path], capture_output=True, text=True, check=False)
    return (out.stdout or "").strip()
