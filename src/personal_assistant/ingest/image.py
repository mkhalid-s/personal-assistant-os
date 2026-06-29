from __future__ import annotations

import shutil
import subprocess


def extract_image_text(image_path: str, manual_text: str = "") -> str:
    if manual_text.strip():
        return manual_text.strip()
    if not shutil.which("tesseract"):
        return ""
    out = subprocess.run(["tesseract", image_path, "stdout"], capture_output=True, text=True, check=False)
    return (out.stdout or "").strip()
