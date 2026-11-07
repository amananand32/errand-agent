"""OpenAI-compatible chat-completions adapter (stdlib only, no SDK needed).

Works with:
  * xAI Grok   - https://api.x.ai/v1                 (XAI_API_KEY)
  * Groq       - https://api.groq.com/openai/v1      (GROQ_API_KEY, free tier, no card)
  * anything else OpenAI-compatible via ERRAND_BASE_URL + ERRAND_API_KEY + ERRAND_MODEL

The agent speaks Anthropic-style blocks internally (text / tool_use / tool_result);
this module converts them to OpenAI messages + tool_calls and back.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

from .base import Model, ModelResponse

PROVIDERS = {
    "grok": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",
             "model_env": "GROK_MODEL", "default_model": "grok-4-1-fast-non-reasoning"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "model_env": "GROQ_MODEL", "default_model": "openai/gpt-oss-120b"},
    "openai-compat": {"base_url": None, "key_env": "ERRAND_API_KEY",
                      "model_env": "ERRAND_MODEL", "default_model": None},
}


# Tried in order when the configured model isn't available to this key (HTTP 404 model_not_found).
# All are tool-capable per Groq's docs (console.groq.com/docs/tool-use/overview, Nov 2026).
FALLBACKS = {
    "groq": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3.8-27b", "openai/gpt-oss-20b",
             "minimaxai/minimax-m2.7", "llama-3.1-8b-instant"],
    "grok": ["grok-4-1-fast-non-reasoning", "grok-4.3", "grok-4.6", "grok-4.5"],
}
NON_CHAT = ("whisper", "orpheus", "guard", "tts", "embed", "image", "vision", "safeguard")


class ModelConfigError(RuntimeError):
    pass


class ModelNotFound(RuntimeError):
    pass


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        if m["role"] == "assistant":
            text = "\n".join(b["text"] for b in c if b.get("type") == "text").strip()
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in c if b.get("type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user turn carrying tool results
            for b in c:
                if b.get("type") == "tool_result":
                    content = b.get("content")
                    if b.get("is_error"):
                        content = f"ERROR: {content}"
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(content)})
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def from_openai_message(msg: dict) -> list[dict]:
    blocks: list[dict] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if not isinstance(args, dict):
                args = {}
        except ValueError:
            args = {"_unparseable_arguments": raw[:500]}
        blocks.append({"type": "tool_use", "id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""),
                       "input": args})
    return blocks


def _ssl_context() -> ssl.SSLContext:
    """Use certifi's CA bundle when available (python.org builds on macOS often lack system CAs)."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_HELP = ("Python could not verify HTTPS certificates. On macOS run "
            "'/Applications/Python 3.x/Install Certificates.command' (python.org installs) "
            "or 'python3 -m pip install certifi', then try again.")


class OpenAICompatModel(Model):
    def __init__(self, provider: str = "groq", model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, max_tokens: int = 2048, temperature: float = 0.0,
                 timeout_s: float = 60.0, max_retries: int = 4):
        cfg = PROVIDERS.get(provider, PROVIDERS["openai-compat"])
        self.base_url = (base_url or os.environ.get("ERRAND_BASE_URL") if provider == "openai-compat"
                         else base_url or cfg["base_url"])
        self.api_key = api_key or os.environ.get(cfg["key_env"], "")
        self.name = model or os.environ.get(cfg["model_env"]) or cfg["default_model"]
        if not self.base_url:
            raise ModelConfigError("set ERRAND_BASE_URL for an OpenAI-compatible provider")
        if not self.api_key:
            raise ModelConfigError(f"missing API key: set {cfg['key_env']} (env var or .env file)")
        if not self.name:
            raise ModelConfigError("set ERRAND_MODEL to the model id")
        self.provider = provider
        self.max_tokens, self.temperature = max_tokens, temperature
        self.timeout_s, self.max_retries = timeout_s, max_retries

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "User-Agent": "errand-agent/0.1"}

    def list_models(self) -> list[str]:
        """Model ids this API key can actually use (GET /models)."""
        req = urllib.request.Request(self.base_url.rstrip("/") + "/models", headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            data = json.loads(r.read())
        return sorted(m["id"] for m in data.get("data", []) if m.get("active", True) is not False)

    def pick_available_model(self) -> str | None:
        """Choose a tool-capable chat model the key has access to."""
        try:
            available = self.list_models()
        except Exception:  # noqa: BLE001
            return None
        for m in FALLBACKS.get(self.provider, []):
            if m in available and m != self.name:
                return m
        chat = [m for m in available if not any(x in m.lower() for x in NON_CHAT) and m != self.name]
        return chat[0] if chat else None

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
            headers=self._headers(), method="POST")
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s, context=_ssl_context()) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:600]
                last = f"HTTP {e.code}: {body}"
                if e.code in (401, 403):
                    raise ModelConfigError(f"{self.provider} rejected the API key ({last})") from e
                if e.code == 404 and "model" in body.lower():
                    raise ModelNotFound(last) from e
                if e.code == 429 or e.code >= 500:
                    wait = float(e.headers.get("retry-after") or 2 ** attempt)
                    time.sleep(min(wait, 20))
                    continue
                raise RuntimeError(last) from e   # 400 etc: the agent loop will retry/handle
            except (urllib.error.URLError, TimeoutError) as e:
                if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                    raise ModelConfigError(SSL_HELP) from e
                last = f"network error: {e}"
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"{self.provider} unavailable after retries: {last}")

    def complete(self, system, messages, tools):
        payload = {"model": self.name, "messages": to_openai_messages(system, messages),
                   "tools": to_openai_tools(tools), "tool_choice": "auto",
                   "max_tokens": self.max_tokens, "temperature": self.temperature}
        try:
            data = self._post(payload)
        except ModelNotFound as e:
            alt = self.pick_available_model()
            if not alt:
                raise ModelConfigError(f"model '{self.name}' is not available to this key and no alternative "
                                       f"was found ({e}). Set {PROVIDERS[self.provider]['model_env']} in .env; "
                                       f"see: python -m errand_agent.check_llm --model {self.provider} --list") from e
            print(f"[errand] model '{self.name}' is not available to this key; switching to '{alt}'")
            self.name = alt
            data = self._post(dict(payload, model=alt))
        except RuntimeError as e:
            if "tool_use_failed" not in str(e) and "failed to call a function" not in str(e).lower():
                raise
            # some open models occasionally emit malformed tool calls; one retry usually fixes it
            data = self._post(dict(payload, temperature=0.4))
        choice = data["choices"][0]
        blocks = from_openai_message(choice.get("message") or {})
        usage = data.get("usage") or {}
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else (choice.get("finish_reason") or "stop")
        return ModelResponse(blocks, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                             stop, self.name, estimated=not usage)
