#!/usr/bin/env python3
"""
captcha_solvers.py - pluggable captcha readers for the e-registration tools.

    from captcha_solvers import list_solvers, solve
    list_solvers()            # [{"id": "openai", "label": "ChatGPT (OpenAI)", "available": True, "reason": ""}, ...]
    solve("openai", png)      # -> "K7X2P"   (raises SolverError with a readable message on failure)

Command line (compare providers on a saved captcha image):
    python captcha_solvers.py captcha.png            # every available solver
    python captcha_solvers.py captcha.png gemini     # one solver
    python captcha_solvers.py --list

Solvers
    manual      - handled by the GUI (the image is shown and you type it); always available
    claude      - Anthropic Messages API           key: ANTHROPIC_API_KEY      model: CAPTCHA_CLAUDE_MODEL
    openai      - OpenAI chat completions (vision) key: OPENAI_API_KEY         model: CAPTCHA_OPENAI_MODEL
    gemini      - Google Gemini generateContent    key: GEMINI_API_KEY         model: CAPTCHA_GEMINI_MODEL
    compatible  - any OpenAI-compatible endpoint (Azure OpenAI, OpenRouter, Groq, LM Studio, vLLM, ...)
                  CAPTCHA_LLM_BASE_URL (e.g. https://openrouter.ai/api/v1), CAPTCHA_LLM_API_KEY, CAPTCHA_LLM_MODEL
    ollama      - local Ollama (free, offline)     CAPTCHA_OLLAMA_URL (default http://localhost:11434),
                  CAPTCHA_OLLAMA_MODEL (default llava;  `ollama pull llava`)
    tesseract   - Tesseract OCR (offline; weak on distorted captchas)  needs `brew install tesseract`,
                  `pip install pytesseract pillow`

Settings come from environment variables first, then from captcha_settings.json next to this file
(copy captcha_settings.example.json). Keys in the JSON are the lower-cased variable names, e.g.
{"openai_api_key": "sk-...", "captcha_openai_model": "gpt-4o-mini"}.

GitHub Copilot has no public API that accepts images from other programs, so it cannot be a solver
here; if your organisation gives you Azure OpenAI or another OpenAI-compatible endpoint, use "compatible".

Only the standard library is required. Everything the LLM sees is the captcha image and a one-line
instruction; nothing else from the page is sent.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "captcha_settings.json"

PROMPT = ("Read the characters in this captcha image. Reply with ONLY the characters, "
          "no spaces, no punctuation, no explanation.")
UPPERCASE = True                    # the e-registration site accepts upper-case; set False to keep case
TIMEOUT = 40

DEFAULTS = {
    "captcha_claude_model": "claude-haiku-4-5-20251001",
    "captcha_openai_model": "gpt-4o-mini",
    "openai_base_url": "https://api.openai.com/v1",
    "captcha_gemini_model": "gemini-2.5-flash",
    "captcha_ollama_url": "http://localhost:11434",
    "captcha_ollama_model": "llava",
    "captcha_llm_base_url": "",
    "captcha_llm_model": "",
}

UI_SETTINGS = {
    "anthropic_api_key": ("ANTHROPIC_API_KEY", True),
    "captcha_claude_model": ("CAPTCHA_CLAUDE_MODEL", False),
    "openai_api_key": ("OPENAI_API_KEY", True),
    "openai_base_url": ("OPENAI_BASE_URL", False),
    "captcha_openai_model": ("CAPTCHA_OPENAI_MODEL", False),
    "gemini_api_key": ("GEMINI_API_KEY", True),
    "captcha_gemini_model": ("CAPTCHA_GEMINI_MODEL", False),
    "captcha_llm_base_url": ("CAPTCHA_LLM_BASE_URL", False),
    "captcha_llm_api_key": ("CAPTCHA_LLM_API_KEY", True),
    "captcha_llm_model": ("CAPTCHA_LLM_MODEL", False),
    "captcha_ollama_url": ("CAPTCHA_OLLAMA_URL", False),
    "captcha_ollama_model": ("CAPTCHA_OLLAMA_MODEL", False),
    "tesseract_cmd": ("TESSERACT_CMD", False),
}


class SolverError(RuntimeError):
    """A solver could not produce an answer (missing key, network, unexpected response...)."""


# ------------------------------------------------------------------ settings
_settings_cache: dict | None = None


def _file_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                _settings_cache = {str(k).lower(): v for k, v in data.items() if v not in (None, "")}
            except Exception as exc:  # noqa: BLE001
                print(f"[captcha_solvers] could not read {SETTINGS_FILE.name}: {exc}", file=sys.stderr)
    return _settings_cache


def setting(name: str, default: str = "") -> str:
    """Environment variable NAME, else captcha_settings.json "name", else built-in default."""
    val = os.environ.get(name.upper())
    if val:
        return val
    val = _file_settings().get(name.lower())
    if val:
        return str(val)
    return DEFAULTS.get(name.lower(), default)


def reload_settings() -> None:
    global _settings_cache
    _settings_cache = None


def user_settings_for_ui() -> dict:
    """Return editable settings and secret-presence flags, never secret values."""
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    values = {}
    configured = {}
    env_overrides = []
    for key, (env_name, secret) in UI_SETTINGS.items():
        env_value = os.environ.get(env_name, "")
        if secret:
            configured[key] = bool(env_value or saved.get(key))
        else:
            values[key] = str(saved.get(key) or DEFAULTS.get(key, ""))
        if env_value:
            env_overrides.append(key)
    if os.environ.get("GOOGLE_API_KEY"):
        configured["gemini_api_key"] = True
        if "gemini_api_key" not in env_overrides:
            env_overrides.append("gemini_api_key")
    return {"values": values, "configured": configured, "env_overrides": env_overrides}


def save_user_settings(values: dict, clear: list[str] | None = None) -> None:
    """Save allowlisted provider settings; blank secret fields leave stored keys unchanged."""
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, json.JSONDecodeError):
        saved = {}

    clear_keys = set(clear or [])
    for key, (_, secret) in UI_SETTINGS.items():
        value = str((values or {}).get(key) or "").strip()
        if key in clear_keys and secret and not value:
            saved.pop(key, None)
            continue
        if key not in (values or {}):
            continue
        if secret:
            if value:
                saved[key] = value
        elif value:
            saved[key] = value
        else:
            saved.pop(key, None)

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        SETTINGS_FILE.chmod(0o600)
    except OSError:
        pass
    reload_settings()


# ------------------------------------------------------------------ HTTP helper
_ssl_ctx: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import truststore  # type: ignore   # use the OS trust store when available (python.org builds)
            _ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:  # noqa: BLE001
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _post_json(url: str, payload: dict, headers: dict, timeout: float = TIMEOUT) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "captcha_solvers/1.0", **headers})
    try:
        ctx = _ssl_context() if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise SolverError(f"HTTP {exc.code} from {url.split('/')[2]}: {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SolverError(f"Could not reach {url.split('/')[2]}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise SolverError(f"Network error talking to {url.split('/')[2]}: {exc}") from None


def _clean(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", text or "")
    return text.upper() if UPPERCASE else text


def _b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


# ------------------------------------------------------------------ providers
def _openai_style(base_url: str, api_key: str, model: str, png: bytes, *, token_field: str = "max_tokens") -> str:
    payload = {
        "model": model,
        token_field: 20,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(png)}},
        ]}],
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _post_json(base_url.rstrip("/") + "/chat/completions", payload, headers)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SolverError(f"Unexpected response: {json.dumps(data)[:300]}") from None
    if isinstance(content, list):                              # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def solve_openai(png: bytes) -> str:
    key = setting("OPENAI_API_KEY")
    if not key:
        raise SolverError("OPENAI_API_KEY is not set")
    base = setting("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return _openai_style(base, key, setting("CAPTCHA_OPENAI_MODEL"), png, token_field="max_completion_tokens")


def solve_compatible(png: bytes) -> str:
    base, model = setting("CAPTCHA_LLM_BASE_URL"), setting("CAPTCHA_LLM_MODEL")
    if not base or not model:
        raise SolverError("CAPTCHA_LLM_BASE_URL and CAPTCHA_LLM_MODEL are not set")
    return _openai_style(base, setting("CAPTCHA_LLM_API_KEY"), model, png)


def solve_claude(png: bytes) -> str:
    key = setting("ANTHROPIC_API_KEY")
    if not key:
        raise SolverError("ANTHROPIC_API_KEY is not set")
    payload = {
        "model": setting("CAPTCHA_CLAUDE_MODEL"),
        "max_tokens": 20,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(png)}},
            {"type": "text", "text": PROMPT},
        ]}],
    }
    data = _post_json("https://api.anthropic.com/v1/messages", payload,
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (KeyError, TypeError):
        raise SolverError(f"Unexpected response: {json.dumps(data)[:300]}") from None


def solve_gemini(png: bytes) -> str:
    key = setting("GEMINI_API_KEY") or setting("GOOGLE_API_KEY")
    if not key:
        raise SolverError("GEMINI_API_KEY is not set")
    model = setting("CAPTCHA_GEMINI_MODEL")
    payload = {
        "contents": [{"parts": [{"text": PROMPT},
                                {"inline_data": {"mime_type": "image/png", "data": _b64(png)}}]}],
        "generationConfig": {"maxOutputTokens": 20, "temperature": 0},
    }
    data = _post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      payload, {"x-goog-api-key": key})
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError):
        raise SolverError(f"Unexpected response: {json.dumps(data)[:300]}") from None


def solve_ollama(png: bytes) -> str:
    url = setting("CAPTCHA_OLLAMA_URL").rstrip("/")
    payload = {"model": setting("CAPTCHA_OLLAMA_MODEL"), "prompt": PROMPT, "images": [_b64(png)],
               "stream": False, "options": {"temperature": 0}}
    data = _post_json(url + "/api/generate", payload, {}, timeout=120)
    if "response" not in data:
        raise SolverError(f"Unexpected response: {json.dumps(data)[:300]}")
    return data["response"]


def solve_tesseract(png: bytes) -> str:
    try:
        import pytesseract  # type: ignore
        from PIL import Image, ImageOps  # type: ignore
    except ImportError:
        raise SolverError("pip install pytesseract pillow  (and install the tesseract binary)") from None
    cmd = setting("TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    img = Image.open(io.BytesIO(png)).convert("L")
    img = img.resize((img.width * 3, img.height * 3))
    img = ImageOps.autocontrast(img).point(lambda p: 255 if p > 140 else 0)
    whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return pytesseract.image_to_string(img, config=f"--psm 7 -c tessedit_char_whitelist={whitelist}")


# ------------------------------------------------------------------ registry
def _ollama_running() -> bool:
    url = setting("CAPTCHA_OLLAMA_URL").rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=1.0):
            return True
    except Exception:  # noqa: BLE001
        return False


SOLVERS: dict[str, dict] = {
    "manual": {"label": "Type it myself", "fn": None,
               "check": lambda: (True, "")},
    "claude": {"label": "Claude (Anthropic)", "fn": solve_claude,
               "check": lambda: (bool(setting("ANTHROPIC_API_KEY")), "set ANTHROPIC_API_KEY")},
    "openai": {"label": "ChatGPT (OpenAI)", "fn": solve_openai,
               "check": lambda: (bool(setting("OPENAI_API_KEY")), "set OPENAI_API_KEY")},
    "gemini": {"label": "Gemini (Google)", "fn": solve_gemini,
               "check": lambda: (bool(setting("GEMINI_API_KEY") or setting("GOOGLE_API_KEY")), "set GEMINI_API_KEY")},
    "compatible": {"label": "Other OpenAI-compatible API", "fn": solve_compatible,
                   "check": lambda: (bool(setting("CAPTCHA_LLM_BASE_URL") and setting("CAPTCHA_LLM_MODEL")),
                                     "set CAPTCHA_LLM_BASE_URL and CAPTCHA_LLM_MODEL")},
    "ollama": {"label": "Ollama (local model)", "fn": solve_ollama,
               "check": lambda: (_ollama_running(), f"start Ollama and `ollama pull {setting('CAPTCHA_OLLAMA_MODEL')}`")},
    "tesseract": {"label": "Tesseract OCR (offline)", "fn": solve_tesseract,
                  "check": lambda: (bool(shutil.which(setting("TESSERACT_CMD") or "tesseract")), "install tesseract + pytesseract")},
}


def list_solvers() -> list[dict]:
    out = []
    for sid, spec in SOLVERS.items():
        ok, reason = spec["check"]()
        out.append({"id": sid, "label": spec["label"], "available": bool(ok), "reason": "" if ok else reason,
                    "model": _model_for(sid)})
    return out


def _model_for(sid: str) -> str:
    return {"claude": setting("CAPTCHA_CLAUDE_MODEL"), "openai": setting("CAPTCHA_OPENAI_MODEL"),
            "gemini": setting("CAPTCHA_GEMINI_MODEL"), "compatible": setting("CAPTCHA_LLM_MODEL"),
            "ollama": setting("CAPTCHA_OLLAMA_MODEL")}.get(sid, "")


def solve(solver_id: str, png: bytes) -> str:
    """Return the cleaned captcha text from the given solver ('' if the model answered nothing usable)."""
    spec = SOLVERS.get(solver_id)
    if spec is None:
        raise SolverError(f"Unknown solver {solver_id!r}; choose from {', '.join(SOLVERS)}")
    if spec["fn"] is None:
        raise SolverError("The manual solver is handled by the GUI")
    raw = spec["fn"](png)
    return _clean(raw)


# ------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        for s in list_solvers():
            state = "ready" if s["available"] else f"unavailable ({s['reason']})"
            print(f"{s['id']:11} {s['label']:32} {s['model'] or '':28} {state}")
        return 0
    png = Path(argv[0]).read_bytes()
    wanted = argv[1:] or [s["id"] for s in list_solvers() if s["available"] and s["id"] != "manual"]
    if not wanted:
        print("No solver is configured. Run with --list to see what each one needs.")
        return 1
    for sid in wanted:
        try:
            print(f"{sid:11} -> {solve(sid, png)!r}")
        except SolverError as exc:
            print(f"{sid:11} -> ERROR: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
