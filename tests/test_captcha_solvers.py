"""captcha_solvers.py tests - the HTTP layer is mocked, so no keys or network are needed.

Run:  python tests/test_captcha_solvers.py
"""
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import captcha_solvers as cs  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n fake"
calls = []


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_urlopen(req, timeout=None, context=None):
    body = json.loads(req.data.decode("utf-8")) if req.data else None
    calls.append({"url": req.full_url, "headers": dict(req.header_items()), "body": body, "timeout": timeout})
    url = req.full_url
    if "api.openai.com" in url or "myproxy" in url:
        reply = {"choices": [{"message": {"content": " k7x2p \n"}}]}
    elif "anthropic" in url:
        reply = {"content": [{"type": "text", "text": "K7-X2P"}]}
    elif "generativelanguage" in url:
        reply = {"candidates": [{"content": {"parts": [{"text": "k7x2p."}]}}]}
    elif "11434" in url:
        reply = {"response": "The text is K7X2P"} if "/api/generate" in url else {"models": []}
    else:
        raise AssertionError("unexpected url " + url)
    return FakeResponse(json.dumps(reply).encode("utf-8"))


urllib.request.urlopen = fake_urlopen
cs.SETTINGS_FILE = Path("/nonexistent/captcha_settings.json")     # ignore any real settings file
cs.reload_settings()
for k in list(os.environ):
    if k.startswith("CAPTCHA_") or k.endswith("_API_KEY"):
        del os.environ[k]

# ---- availability without keys
avail = {s["id"]: s["available"] for s in cs.list_solvers()}
assert avail["manual"] is True and avail["claude"] is False and avail["openai"] is False and avail["gemini"] is False, avail

# ---- OpenAI
os.environ["OPENAI_API_KEY"] = "sk-test"
assert cs.solve("openai", PNG) == "K7X2P"
c = calls[-1]
assert c["url"] == "https://api.openai.com/v1/chat/completions"
assert c["headers"].get("Authorization") == "Bearer sk-test"
assert c["body"]["model"] == "gpt-4o-mini" and c["body"]["max_completion_tokens"] == 20
parts = c["body"]["messages"][0]["content"]
assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

# ---- Claude
os.environ["ANTHROPIC_API_KEY"] = "ak-test"
os.environ["CAPTCHA_CLAUDE_MODEL"] = "claude-test-model"
assert cs.solve("claude", PNG) == "K7X2P"
c = calls[-1]
assert c["url"] == "https://api.anthropic.com/v1/messages" and c["headers"].get("X-api-key") == "ak-test"
assert c["headers"].get("Anthropic-version") == "2023-06-01" and c["body"]["model"] == "claude-test-model"
assert c["body"]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"

# ---- Gemini
os.environ["GEMINI_API_KEY"] = "g-test"
assert cs.solve("gemini", PNG) == "K7X2P"
c = calls[-1]
assert c["url"].endswith("/models/gemini-2.5-flash:generateContent") and c["headers"].get("X-goog-api-key") == "g-test"
assert c["body"]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"

# ---- OpenAI-compatible endpoint (e.g. Azure / OpenRouter / LM Studio)
os.environ["CAPTCHA_LLM_BASE_URL"] = "https://myproxy.example.com/v1/"
os.environ["CAPTCHA_LLM_MODEL"] = "some-vision-model"
os.environ["CAPTCHA_LLM_API_KEY"] = "p-test"
assert cs.solve("compatible", PNG) == "K7X2P"
c = calls[-1]
assert c["url"] == "https://myproxy.example.com/v1/chat/completions" and c["body"]["max_tokens"] == 20
assert c["body"]["model"] == "some-vision-model" and c["headers"].get("Authorization") == "Bearer p-test"

# ---- Ollama
assert cs.solve("ollama", PNG) == "THETEXTISK7X2P"             # cleaned; a chatty model needs a stricter prompt/model
c = calls[-1]
assert c["url"] == "http://localhost:11434/api/generate" and c["body"]["model"] == "llava" and c["body"]["stream"] is False
assert cs.list_solvers()[5]["id"] == "ollama" and cs.list_solvers()[5]["available"] is True   # /api/tags answered

# ---- unknown / manual
for bad in ("nope", "manual"):
    try:
        cs.solve(bad, PNG); raise AssertionError("should have raised")
    except cs.SolverError:
        pass

# ---- settings file fallback and env override
import tempfile  # noqa: E402
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d) / "captcha_settings.json"
    tmp.write_text(json.dumps({"openai_api_key": "from-file", "captcha_openai_model": "gpt-file"}), encoding="utf-8")
    cs.SETTINGS_FILE = tmp
    cs.reload_settings()
    del os.environ["OPENAI_API_KEY"]
    assert cs.setting("OPENAI_API_KEY") == "from-file" and cs.setting("CAPTCHA_OPENAI_MODEL") == "gpt-file"
    ui_config = cs.user_settings_for_ui()
    assert ui_config["configured"]["openai_api_key"] is True
    assert "from-file" not in json.dumps(ui_config)
    cs.save_user_settings({"openai_api_key": "", "captcha_openai_model": "gpt-saved", "unknown": "ignored"})
    assert cs.setting("OPENAI_API_KEY") == "from-file"
    assert cs.setting("CAPTCHA_OPENAI_MODEL") == "gpt-saved"
    assert "unknown" not in json.loads(tmp.read_text(encoding="utf-8"))
    cs.save_user_settings({"openai_api_key": "rotated-key"})
    assert cs.setting("OPENAI_API_KEY") == "rotated-key"
    assert "rotated-key" not in json.dumps(cs.user_settings_for_ui())
    cs.save_user_settings({"openai_api_key": "replacement-key"}, ["openai_api_key"])
    assert cs.setting("OPENAI_API_KEY") == "replacement-key"
    cs.save_user_settings({}, ["openai_api_key"])
    assert "openai_api_key" not in json.loads(tmp.read_text(encoding="utf-8"))
    os.environ["CAPTCHA_OPENAI_MODEL"] = "gpt-env"
    assert cs.setting("CAPTCHA_OPENAI_MODEL") == "gpt-env"

print("captcha_solvers: ALL TESTS PASSED")
