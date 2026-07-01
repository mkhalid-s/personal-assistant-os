from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import resolve_db_path

ROUTER_MODELS: dict[str, dict[str, str]] = {
    "qwen2.5:0.5b": {
        "label": "Qwen2.5 0.5B Instruct",
        "footprint": "~400 MB model, about 1-2 GB runtime memory depending on runtime/context",
        "quality": "recommended tiny default for intent routing accuracy",
    },
    "smollm2:360m": {
        "label": "SmolLM2 360M Instruct",
        "footprint": "~250-300 MB model, about 1-1.5 GB runtime memory depending on runtime/context",
        "quality": "lower-memory fallback for very small machines",
    },
}
RUNTIMES = ("ollama", "llama-cpp", "command")
DEFAULT_ROUTER_MODEL = "qwen2.5:0.5b"
DEFAULT_ROUTER_TIMEOUT = "8"
DEFAULT_ROUTER_MIN_CONFIDENCE = "0.70"


@dataclass(frozen=True)
class RuntimeStatus:
    runtime: str
    available: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"runtime": self.runtime, "available": self.available, "detail": self.detail}


def recommended_model(purpose: str = "router") -> dict[str, str]:
    if purpose != "router":
        raise ValueError(f"unsupported model purpose: {purpose}")
    model = DEFAULT_ROUTER_MODEL
    return {"purpose": purpose, "model": model, **ROUTER_MODELS[model]}


def validate_model(model: str) -> str:
    model = (model or DEFAULT_ROUTER_MODEL).strip()
    if model not in ROUTER_MODELS:
        allowed = ", ".join(sorted(ROUTER_MODELS))
        raise ValueError(f"unsupported router model: {model}. Allowed: {allowed}")
    return model


def detect_runtime(preferred: str = "auto") -> RuntimeStatus:
    preferred = (preferred or "auto").strip()
    if preferred not in {"auto", *RUNTIMES}:
        raise ValueError(f"unsupported runtime: {preferred}")
    if preferred == "command":
        command = os.getenv("MYOS_ROUTER_COMMAND", "").strip()
        return RuntimeStatus("command", bool(command), command or "MYOS_ROUTER_COMMAND is not configured")
    if preferred in {"auto", "ollama"}:
        ollama = shutil.which("ollama")
        if ollama:
            return RuntimeStatus("ollama", True, ollama)
        if preferred == "ollama":
            return RuntimeStatus("ollama", False, "ollama not found on PATH")
    if preferred in {"auto", "llama-cpp"}:
        exe = shutil.which("llama-server") or shutil.which("llama-cli")
        if exe:
            return RuntimeStatus("llama-cpp", True, exe)
        if preferred == "llama-cpp":
            return RuntimeStatus("llama-cpp", False, "llama-server or llama-cli not found on PATH")
    return RuntimeStatus("ollama", False, "no supported local runtime found; install Ollama or configure MYOS_ROUTER_COMMAND")


def router_wrapper_path(runtime: str) -> Path:
    data_dir = resolve_db_path().parent
    return data_dir / "router" / f"router_{runtime.replace('-', '_')}.py"


def _wrapper_source(runtime: str) -> str:
    if runtime == "ollama":
        return r'''from __future__ import annotations

import json
import os
import sys
import urllib.request

schema = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "recommended_workflow": {"type": "string"},
        "requires_confirmation": {"type": "boolean"},
        "command_tier": {"type": "string"},
        "workflow_pack": {"type": "string"},
    },
    "required": ["intent", "confidence", "reason", "recommended_workflow"],
}

request = json.loads(sys.stdin.read() or "{}")
allowed = ", ".join(request.get("allowed_intents", []))
catalog = request.get("command_catalog", [])
commands = "; ".join(
    f"{item.get('command')}[{item.get('tier')}/{item.get('safety')}]: {item.get('summary')}"
    for item in catalog[:24]
    if isinstance(item, dict)
)
prompt = (
    "Classify this MYOS user request into one allowed intent. "
    "Return JSON only. Allowed intents: " + allowed + "\n"
    "Available MYOS commands: " + commands + "\n"
    "User text: " + str(request.get("text", ""))
)
payload = {
    "model": os.getenv("MYOS_ROUTER_MODEL", "qwen2.5:0.5b"),
    "stream": False,
    "format": schema,
    "options": {"temperature": 0},
    "messages": [{"role": "user", "content": prompt}],
}
url = os.getenv("MYOS_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=int(os.getenv("MYOS_ROUTER_TIMEOUT_SEC", "8"))) as resp:
    data = json.loads(resp.read().decode("utf-8"))
content = (data.get("message") or {}).get("content") or data.get("response") or "{}"
print(content)
'''
    if runtime == "llama-cpp":
        return r'''from __future__ import annotations

import json
import os
import sys
import urllib.request

request = json.loads(sys.stdin.read() or "{}")
allowed = ", ".join(request.get("allowed_intents", []))
catalog = request.get("command_catalog", [])
commands = "; ".join(
    f"{item.get('command')}[{item.get('tier')}/{item.get('safety')}]: {item.get('summary')}"
    for item in catalog[:24]
    if isinstance(item, dict)
)
prompt = (
    "Classify this MYOS user request into one allowed intent. "
    "Return JSON only with intent, confidence, reason, recommended_workflow, "
    "requires_confirmation, command_tier, workflow_pack. Allowed intents: " + allowed + "\n"
    "Available MYOS commands: " + commands + "\n"
    "User text: " + str(request.get("text", ""))
)
payload = {
    "model": os.getenv("MYOS_ROUTER_MODEL", "qwen2.5:0.5b"),
    "temperature": 0,
    "messages": [{"role": "user", "content": prompt}],
    "response_format": {"type": "json_object"},
}
url = os.getenv("MYOS_LLAMA_CPP_URL", "http://127.0.0.1:8080/v1/chat/completions")
req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=int(os.getenv("MYOS_ROUTER_TIMEOUT_SEC", "8"))) as resp:
    data = json.loads(resp.read().decode("utf-8"))
content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
print(content)
'''
    return ""


def write_wrapper(runtime: str, *, force: bool = False) -> Path | None:
    source = _wrapper_source(runtime)
    if not source:
        return None
    path = router_wrapper_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return path
    path.write_text(source)
    path.chmod(0o700)
    return path


def env_lines(*, runtime: str, model: str, command: str = "") -> list[str]:
    model = validate_model(model)
    runtime = runtime if runtime in RUNTIMES else "ollama"
    if not command:
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(router_wrapper_path(runtime)))}"
    return [
        f"MYOS_ROUTER_BACKEND={runtime}",
        f"MYOS_ROUTER_MODEL={model}",
        f"MYOS_ROUTER_COMMAND={command}",
        f"MYOS_ROUTER_TIMEOUT_SEC={DEFAULT_ROUTER_TIMEOUT}",
        f"MYOS_ROUTER_MIN_CONFIDENCE={DEFAULT_ROUTER_MIN_CONFIDENCE}",
    ]


def pull_command(runtime: str, model: str) -> list[str]:
    model = validate_model(model)
    if runtime == "ollama":
        return ["ollama", "pull", model]
    if runtime == "llama-cpp":
        return []
    if runtime == "command":
        return []
    raise ValueError(f"unsupported runtime: {runtime}")


def setup_plan(*, runtime: str = "auto", model: str = "", command: str = "") -> dict[str, Any]:
    selected = validate_model(model or DEFAULT_ROUTER_MODEL)
    status = detect_runtime(runtime)
    selected_runtime = status.runtime if runtime == "auto" else runtime
    runtime_available = bool(command) if selected_runtime == "command" and command else status.available
    runtime_detail = command if selected_runtime == "command" and command else status.detail
    pull = pull_command(selected_runtime, selected)
    lines = env_lines(runtime=selected_runtime, model=selected, command=command)
    info = ROUTER_MODELS[selected]
    return {
        "purpose": "router",
        "runtime": selected_runtime,
        "runtime_available": runtime_available,
        "runtime_detail": runtime_detail,
        "model": selected,
        "model_label": info["label"],
        "footprint": info["footprint"],
        "quality": info["quality"],
        "pull_command": pull,
        "pull_command_text": " ".join(shlex.quote(part) for part in pull) if pull else "manual/custom setup required",
        "env_lines": lines,
        "wrapper_path": str(router_wrapper_path(selected_runtime)),
        "privacy_note": "Router setup stores configuration only; raw user text is sent to the local router command at routing time and is not persisted by setup.",
    }


def apply_setup(plan: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    command = list(plan.get("pull_command") or [])
    if dry_run or not command:
        wrapper = None if dry_run else write_wrapper(str(plan.get("runtime") or ""))
        status = "dry_run" if dry_run else "ok"
        return {"status": status, "command": command, "stdout": "", "stderr": "", "wrapper": str(wrapper) if wrapper else ""}
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    wrapper = write_wrapper(str(plan.get("runtime") or ""))
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "command": command,
        "wrapper": str(wrapper) if wrapper else "",
        "stdout": (proc.stdout or "")[:2000],
        "stderr": (proc.stderr or "")[:2000],
    }


def router_status() -> dict[str, Any]:
    backend = os.getenv("MYOS_ROUTER_BACKEND", "").strip() or "heuristic"
    model = os.getenv("MYOS_ROUTER_MODEL", "").strip()
    command = os.getenv("MYOS_ROUTER_COMMAND", "").strip()
    runtime = detect_runtime(backend if backend in RUNTIMES else "command" if command else "auto")
    return {
        "backend": backend,
        "model": model or "not configured",
        "command": command or "not configured",
        "runtime": runtime.runtime,
        "available": bool(command) and runtime.available if backend == "command" else runtime.available,
        "detail": runtime.detail,
    }
