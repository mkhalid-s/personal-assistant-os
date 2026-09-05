from __future__ import annotations

import subprocess
import sys


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
    # PAOS-025: run the helper with the SAME interpreter that is running MYOS —
    # a bare "python3" may be a different install without faster_whisper (or a
    # venv-less system python), silently degrading transcription.
    out = subprocess.run([sys.executable, "-c", py, audio_path], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        # Surface the stderr snippet on stderr but keep the "" return contract:
        # the caller already distinguishes "no transcript" from success.
        snippet = " | ".join((out.stderr or "").strip().splitlines()[:2])[:200]
        print(f"audio transcription helper failed rc={out.returncode}: {snippet}", file=sys.stderr)
    return (out.stdout or "").strip()
