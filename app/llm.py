"""Every LLM call goes through complete(). The provider and model come from config.llm_choice()
(the open session's choice, else LLM_PROVIDER / LLM_MODEL).

Providers: anthropic (default), openai, gemini, ollama, openai_compatible (LM Studio, llama.cpp,
vLLM...), claude_code (the Claude Code CLI, `claude -p`, on the user's subscription login).
Callers always pass Anthropic-style content (a string, or a list of text and base64 image
blocks); each adapter translates it. No sampling parameters are sent to any provider.

The OpenAI and Gemini SDKs are optional imports: the app starts without them, and choosing a
provider whose SDK is missing raises ProviderUnavailable with the pip command.
"""

import base64
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
from functools import lru_cache

import anthropic
import httpx

from . import config
from .config import WORKSPACE_ID


class LLMError(RuntimeError):
    """A call that cannot produce a usable answer (refusal, truncation, malformed JSON...)."""


class ProviderUnavailable(LLMError):
    """The provider cannot be used as configured: missing SDK, key, server or model."""


class VisionUnsupported(LLMError):
    """Images were sent to a model that cannot read them."""


_SDK = {"openai": ("openai", "openai"), "openai_compatible": ("openai", "openai"),
        "gemini": ("google.genai", "google-genai")}


def sdk_missing(provider):
    """The pip package a provider needs and lacks, or None."""
    mod, pip = _SDK.get(provider, (None, None))
    if mod is None:
        return None
    try:
        found = importlib.util.find_spec(mod) is not None
    except ModuleNotFoundError:                  # find_spec("google.genai") without "google"
        found = False
    return None if found else pip


def _require(provider):
    pip = sdk_missing(provider)
    if pip:
        raise ProviderUnavailable(f"{config.LLM_PROVIDERS[provider]} needs the {pip!r} package: "
                                  f".venv/bin/pip install {pip}")


# ---------------------------------------------------------------- content helpers

def _blocks(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _has_images(content):
    return not isinstance(content, str) and any(b.get("type") == "image" for b in content)


# ---------------------------------------------------------------- anthropic

@lru_cache(maxsize=1)
def client():
    return anthropic.Anthropic(max_retries=4)


def _anthropic(model, content, system, max_tokens, json_schema, stream):
    kwargs = dict(model=model, max_tokens=max_tokens,
                  messages=[{"role": "user", "content": content}])
    if system:
        kwargs["system"] = system
    if json_schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    if WORKSPACE_ID:
        kwargs["workspace_id"] = WORKSPACE_ID
    if stream:
        with client().messages.stream(**kwargs) as s:
            resp = s.get_final_message()
    else:
        resp = client().messages.create(**kwargs)
    return {"text": "".join(b.text for b in resp.content if b.type == "text"),
            "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
            "stop_reason": resp.stop_reason, "detail": None}


# ---------------------------------------------------------------- openai + openai-compatible

@lru_cache(maxsize=4)
def openai_client(base_url=None, api_key=None):
    _require("openai")
    import openai
    return openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=4)


def _openai_conn(provider):
    if provider == "openai":
        if not config.OPENAI_API_KEY:
            raise ProviderUnavailable("OpenAI needs OPENAI_API_KEY in full-workflow-api/.env")
        return openai_client(None, config.OPENAI_API_KEY)
    return openai_client(config.LOCAL_LLM_BASE_URL, config.LOCAL_LLM_API_KEY)


_OPENAI_STOP = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal",
                "tool_calls": "tool_use"}


def _openai(model, content, system, max_tokens, json_schema, stream, provider="openai"):
    parts = []
    for b in _blocks(content):
        if b["type"] == "text":
            parts.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            src = b["source"]
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": content if isinstance(content, str) else parts}]
    kwargs = dict(model=model, messages=messages)
    # OpenAI's current name for the cap; local servers mostly still read max_tokens.
    kwargs["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens
    if json_schema:
        kwargs["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": "output", "schema": json_schema, "strict": True}}
    try:
        resp = _openai_conn(provider).chat.completions.create(**kwargs)
    except Exception as e:
        if type(e).__name__ == "APIConnectionError":
            raise ProviderUnavailable(f"no OpenAI-compatible server at {config.LOCAL_LLM_BASE_URL} "
                                      "(start LM Studio's server, or set LOCAL_LLM_BASE_URL)") from e
        raise
    if not resp.choices:
        return {"text": "", "input_tokens": None, "output_tokens": None, "stop_reason": "refusal",
                "detail": "no choices returned"}
    ch = resp.choices[0]
    refusal = getattr(ch.message, "refusal", None)
    u = resp.usage
    return {"text": ch.message.content or "",
            "input_tokens": getattr(u, "prompt_tokens", None), "output_tokens": getattr(u, "completion_tokens", None),
            "stop_reason": "refusal" if refusal else _OPENAI_STOP.get(ch.finish_reason, ch.finish_reason),
            "detail": refusal}


# ---------------------------------------------------------------- gemini

@lru_cache(maxsize=1)
def gemini_client():
    _require("gemini")
    if not config.GEMINI_API_KEY:
        raise ProviderUnavailable("Gemini needs GEMINI_API_KEY (or GOOGLE_API_KEY) in full-workflow-api/.env")
    from google import genai
    return genai.Client(api_key=config.GEMINI_API_KEY)


_GEMINI_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
                   "IMAGE_PROHIBITED_CONTENT", "MODEL_ARMOR", "LANGUAGE"}


def _name(enum):
    return None if enum is None else getattr(enum, "name", str(enum))


def _gemini(model, content, system, max_tokens, json_schema, stream):
    gc = gemini_client()
    from google.genai import types
    parts = []
    for b in _blocks(content):
        if b["type"] == "text":
            parts.append(types.Part.from_text(text=b["text"]))
        elif b["type"] == "image":
            parts.append(types.Part.from_bytes(data=base64.b64decode(b["source"]["data"]),
                                               mime_type=b["source"]["media_type"]))
    cfg = dict(max_output_tokens=max_tokens)
    if system:
        cfg["system_instruction"] = system
    if json_schema:
        cfg.update(response_mime_type="application/json", response_json_schema=json_schema)
    resp = gc.models.generate_content(model=model, contents=[types.Content(role="user", parts=parts)],
                                      config=types.GenerateContentConfig(**cfg))
    u = resp.usage_metadata
    usage = {"input_tokens": getattr(u, "prompt_token_count", None),
             "output_tokens": (getattr(u, "candidates_token_count", None) or 0) + (getattr(u, "thoughts_token_count", None) or 0)
             if u else None}
    blocked = _name(getattr(resp.prompt_feedback, "block_reason", None)) if resp.prompt_feedback else None
    if blocked or not resp.candidates:
        return {"text": "", **usage, "stop_reason": "refusal", "detail": f"prompt blocked: {blocked}"}
    cand = resp.candidates[0]
    finish = _name(cand.finish_reason)
    text = "".join(p.text for p in ((cand.content and cand.content.parts) or [])
                   if getattr(p, "text", None) and not getattr(p, "thought", False))
    stop = ("refusal" if finish in _GEMINI_BLOCKED else "max_tokens" if finish == "MAX_TOKENS"
            else "end_turn" if finish in (None, "STOP") else finish.lower())
    return {"text": text, **usage, "stop_reason": stop, "detail": finish if stop == "refusal" else None}


# ---------------------------------------------------------------- ollama (native API)

def _ollama_http(path, body=None, timeout=600.0):
    url = config.OLLAMA_BASE_URL + path
    try:
        r = (httpx.post(url, json=body, timeout=timeout) if body is not None
             else httpx.get(url, timeout=timeout))
    except httpx.HTTPError as e:
        raise ProviderUnavailable(f"Ollama is not reachable at {config.OLLAMA_BASE_URL} "
                                  "(start the Ollama app or `ollama serve`, or set OLLAMA_BASE_URL)") from e
    if r.status_code == 404 and body and body.get("model"):
        raise ProviderUnavailable(f"Ollama has no model {body['model']!r}: run `ollama pull {body['model']}`")
    if r.status_code >= 400:
        raise LLMError(f"Ollama {path} returned {r.status_code}: {r.text[:300]}")
    return r.json()


def ollama_capabilities(model):
    """Ollama's capability list for a model (e.g. ["completion", "vision"]), or None if it does not say."""
    return _ollama_http("/api/show", {"model": model}, timeout=30.0).get("capabilities")


def _ollama(model, content, system, max_tokens, json_schema, stream):
    if _has_images(content):
        caps = ollama_capabilities(model)
        if caps is not None and "vision" not in caps:
            raise VisionUnsupported(f"Ollama model {model!r} cannot read images (capabilities: "
                                    f"{', '.join(caps)}); pick a vision model, e.g. `ollama pull qwen2.5vl`")
    # Ollama takes one string plus a list of images, so mark where each image sat in the text.
    texts, images = [], []
    for b in _blocks(content):
        if b["type"] == "text":
            texts.append(b["text"])
        elif b["type"] == "image":
            images.append(b["source"]["data"])
            texts.append(f"[image {len(images)}]")
    user = {"role": "user", "content": "\n".join(texts)}
    if images:
        user["images"] = images
    body = {"model": model, "stream": False,
            "messages": ([{"role": "system", "content": system}] if system else []) + [user],
            # output cap and context size, not sampling settings
            "options": {"num_predict": max_tokens, "num_ctx": config.OLLAMA_NUM_CTX}}
    if json_schema:
        body["format"] = json_schema
    r = _ollama_http("/api/chat", body)
    pin, pout = r.get("prompt_eval_count"), r.get("eval_count")
    if pin is not None and pin + (pout or 0) >= config.OLLAMA_NUM_CTX:
        raise LLMError(f"the prompt filled Ollama's {config.OLLAMA_NUM_CTX}-token context and was cut; "
                       "raise OLLAMA_NUM_CTX")
    done = r.get("done_reason")
    return {"text": (r.get("message") or {}).get("content") or "", "input_tokens": pin, "output_tokens": pout,
            "stop_reason": {"stop": "end_turn", "length": "max_tokens", None: "end_turn"}.get(done, done),
            "detail": None}


# ---------------------------------------------------------------- claude code CLI (subscription login)

class RateLimited(LLMError):
    """The Claude Code login hit a rate or usage limit. resets_at: epoch seconds, when known."""

    def __init__(self, msg, resets_at=None):
        super().__init__(msg)
        self.resets_at = resets_at


# Clean context: the scoring call must see only our prompt. --bare would be cleaner but drops the
# subscription (OAuth) login, so each source of user context is switched off one by one instead:
# --safe-mode (CLAUDE.md, user/plugin hooks, skills, MCP, memory, output styles), no settings files,
# no MCP servers, no tools, no slash commands, nothing saved to disk. --system-prompt replaces Claude
# Code's agent prompt (an empty one when the caller has none). What still reaches the model is what
# the CLI always adds and no flag removes: a one-line identity ("You are a Claude agent, built on
# Anthropic's Claude Agent SDK."), the account e-mail reminder and an environment block (cwd = an
# empty temp dir, OS, date, model id). See README "Claude Code CLI" for the probe that checked this.
_CC_FLAGS = ["-p", "--safe-mode", "--setting-sources", "", "--strict-mcp-config", "--tools", "",
             "--disable-slash-commands", "--no-session-persistence",
             "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
# Only what the CLI needs to run and find its login. Never ANTHROPIC_API_KEY (an API key wins over
# the subscription login: a dead key would 401) nor CLAUDECODE / CLAUDE_CODE_* of a parent session.
_CC_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SHELL", "TERM")
_CC_LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|hit your limit|overloaded|too many requests",
                          re.IGNORECASE)
_CC_QUOTA_RE = re.compile(r"usage limit|limit reached|hit your limit", re.IGNORECASE)   # subscription window, not a 429 blip
_CC_EPOCH_RE = re.compile(r"\|\s*(\d{10})\b")
_CC_LOGIN_RE = re.compile(r"not logged in|/login|invalid api key|authentication|oauth", re.IGNORECASE)


def _cc_env():
    return {k: os.environ[k] for k in _CC_ENV if k in os.environ}


def _cc_command(model, system, json_schema):
    cmd = [config.CLAUDE_CODE_BIN, *_CC_FLAGS, "--system-prompt", system or "", "--model", model]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    return cmd


def _cc_input(content):
    """One stream-json user message. Content blocks (text, base64 images) are Anthropic-shaped already."""
    blocks = [b for b in _blocks(content) if b.get("type") in ("text", "image")]
    return json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"


def _cc_once(model, content, system, json_schema):
    """One `claude -p` run. Returns the adapter dict; raises LLMError / RateLimited / ProviderUnavailable."""
    if not os.path.isfile(config.CLAUDE_CODE_BIN):
        raise ProviderUnavailable(f"Claude Code CLI not found at {config.CLAUDE_CODE_BIN}: install it or set CLAUDE_CODE_BIN")
    with tempfile.TemporaryDirectory(prefix="partgram-cc-") as cwd:          # no CLAUDE.md / AGENTS.md / git here
        try:
            proc = subprocess.run(_cc_command(model, system, json_schema), input=_cc_input(content),
                                  capture_output=True, text=True, cwd=cwd, env=_cc_env(),
                                  timeout=config.CLAUDE_CODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude -p did not answer within {config.CLAUDE_CODE_TIMEOUT_S:g}s") from None
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue                                                      # stray non-JSON output
    limit = next((e.get("rate_limit_info") or {} for e in reversed(events) if e.get("type") == "rate_limit_event"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        if _CC_LOGIN_RE.search(tail):
            raise ProviderUnavailable(f"Claude Code CLI is not logged in (run `claude`, then /login): {tail}")
        raise LLMError(f"claude -p exited {proc.returncode} without a result: {tail or 'no output'}")
    text = (result.get("result") or "").strip()
    status = result.get("api_error_status")
    if result.get("is_error") or result.get("subtype") != "success":
        msg = f"claude -p error (status {status}, {result.get('subtype')}): {text[:300] or 'no message'}"
        if status == 429 or (status or 0) >= 500 or _CC_LIMIT_RE.search(text) or limit.get("status") == "rejected":
            resets = limit.get("resetsAt")
            if resets is None and (m := _CC_EPOCH_RE.search(text)):
                resets = int(m.group(1))                                  # "Claude AI usage limit reached|<epoch>"
            if resets is None and _CC_QUOTA_RE.search(text):
                resets = time.time() + config.CLAUDE_CODE_MAX_WAIT_S + 1   # a quota, not a blip: stop, don't sleep
            raise RateLimited(msg, resets)
        if status in (401, 403) or _CC_LOGIN_RE.search(text):
            raise ProviderUnavailable(f"Claude Code CLI login problem (run `claude`, then /login): {msg}")
        if status == 404:
            raise ProviderUnavailable(f"Claude Code cannot use model {model!r}: {msg}")
        raise LLMError(msg)
    stop = result.get("stop_reason")
    if json_schema:
        structured = result.get("structured_output")
        if structured is not None:
            text, stop = json.dumps(structured, ensure_ascii=False), "end_turn"   # parse_json still validates it
    if stop != "refusal" and not text:
        raise LLMError("claude -p returned an empty answer")
    u = result.get("usage") or {}
    pin = None if u.get("input_tokens") is None else sum(
        u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"text": text, "input_tokens": pin, "output_tokens": u.get("output_tokens"),
            "stop_reason": {"end_turn": "end_turn", "max_tokens": "max_tokens", "refusal": "refusal",
                            None: "end_turn"}.get(stop, stop),
            "detail": text[:300] if stop == "refusal" else None,
            "cost_usd": result.get("total_cost_usd")}    # list-price equivalent; a subscription is not billed per call


def _claude_code(model, content, system, max_tokens, json_schema, stream):
    """The CLI has no output cap flag (max_tokens is not sent) and no sampling flags; stream is moot.
    Rate limits and overload are retried with backoff; a usage limit that resets later than
    CLAUDE_CODE_MAX_WAIT_S is raised at once so a batch can stop instead of sleeping for hours."""
    for attempt in range(config.CLAUDE_CODE_RETRIES + 1):
        try:
            return _cc_once(model, content, system, json_schema)
        except RateLimited as e:
            wait = min(30 * 2 ** attempt, 300)
            if e.resets_at and e.resets_at - time.time() > config.CLAUDE_CODE_MAX_WAIT_S:
                raise
            if attempt == config.CLAUDE_CODE_RETRIES:
                raise
            time.sleep(wait)


_ADAPTERS = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini, "ollama": _ollama,
             "openai_compatible": lambda *a: _openai(*a, provider="openai_compatible"),
             "claude_code": _claude_code}


def complete(content, system=None, max_tokens=1500, json_schema=None, stream=False):
    """Single user turn. `content` is a string or a list of content blocks (text + images).

    stream=True is for long outputs on Anthropic: the reply arrives as it is written, so a
    request that takes minutes is not dropped as an idle connection. Other providers ignore it.

    Returns {"text", "input_tokens", "output_tokens", "latency_s", "provider", "model", "stop_reason",
    "cost_usd"}. Token counts are None when a local server does not report them (never 0); cost_usd is
    None unless the provider reports it (the Claude Code CLI does).
    """
    choice = config.llm_choice()
    provider, model = choice["provider"], choice["model"]
    if not model:
        raise ProviderUnavailable(f"no model chosen for {config.LLM_PROVIDERS[provider]}: pick one in the "
                                  "Run panel, or set LLM_MODEL")
    _require(provider)
    t0 = time.time()
    out = _ADAPTERS[provider](model, content, system, max_tokens, json_schema, stream)
    if out["stop_reason"] == "refusal":
        why = f": {out['detail']}" if out.get("detail") else ""
        raise RuntimeError(f"the model declined this request (stop_reason=refusal){why}")
    if out["stop_reason"] == "max_tokens" and not out["text"].strip():
        raise LLMError(f"{model} used its whole {max_tokens}-token output budget without answering "
                       "(reasoning models count their thinking against it)")
    return {"text": out["text"], "input_tokens": out["input_tokens"], "output_tokens": out["output_tokens"],
            "latency_s": round(time.time() - t0, 2), "provider": provider, "model": model,
            "stop_reason": out["stop_reason"], "cost_usd": out.get("cost_usd")}


USAGE_KEYS = ("input_tokens", "output_tokens", "latency_s", "provider", "model", "cost_usd")


def usage(resp):
    """The part of a complete() result that is saved with each stage."""
    return {k: resp.get(k) for k in USAGE_KEYS}


# ---------------------------------------------------------------- strict JSON output

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _check(value, schema, path="$"):
    """A small JSON-schema check (type, enum, required, properties, items, anyOf): enough to
    reject a local model that ignored the requested format. Returns the first problem or None."""
    if "anyOf" in schema:
        return None if any(_check(value, s, path) is None for s in schema["anyOf"]) else f"{path}: matches no allowed type"
    t = schema.get("type")
    ok = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
          "boolean": isinstance(value, bool), "null": value is None,
          "number": isinstance(value, (int, float)) and not isinstance(value, bool),
          "integer": isinstance(value, int) and not isinstance(value, bool)}.get(t, True)
    if not ok:
        return f"{path}: expected {t}, got {type(value).__name__}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: {value!r} is not one of {schema['enum']}"
    if t == "object":
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            return f"{path}: missing {', '.join(missing)}"
        for k, sub in schema.get("properties", {}).items():
            if k in value and (err := _check(value[k], sub, f"{path}.{k}")):
                return err
    if t == "array" and "items" in schema:
        for i, item in enumerate(value):
            if err := _check(item, schema["items"], f"{path}[{i}]"):
                return err
    return None


def parse_json(text, schema=None):
    """Parse a structured reply strictly. Raises LLMError on malformed JSON or a schema mismatch."""
    s = (text or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1)
    try:
        data = json.loads(s)
    except ValueError as e:
        raise LLMError(f"the model returned malformed JSON ({e}): {s[:200]!r}") from None
    if schema and (err := _check(data, schema)):
        raise LLMError(f"the model's JSON does not match the expected schema: {err}")
    return data


# ---------------------------------------------------------------- model lists (for the UI)

_OPENAI_SKIP = re.compile(r"embed|tts|whisper|dall-e|image|audio|realtime|moderation|transcribe|davinci|babbage|search",
                          re.IGNORECASE)


def _models(provider):
    """[{"id", "vision"}] for one provider. vision is True / False / None (unknown)."""
    if provider == "anthropic":
        return [{"id": m.id, "vision": True} for m in client().models.list()]   # free call
    if provider in ("openai", "openai_compatible"):
        _require(provider)
        ids = sorted(m.id for m in _openai_conn(provider).models.list())
        if provider == "openai":
            ids = [i for i in ids if not _OPENAI_SKIP.search(i)]
        return [{"id": i, "vision": None} for i in ids]
    if provider == "gemini":
        return [{"id": m.name.removeprefix("models/"), "vision": True} for m in gemini_client().models.list()
                if "generateContent" in (m.supported_actions or [])]
    if provider == "claude_code":
        # The CLI has no model listing; these are the ids it accepts (aliases work too: opus, sonnet).
        ids = dict.fromkeys([config.DEFAULT_MODELS["claude_code"], "claude-opus-5-5", "claude-sonnet-5-5",
                             "claude-haiku-5-5", "claude-fable-5-2"])
        return [{"id": i, "vision": True} for i in ids]
    if provider == "ollama":
        out = []
        for m in _ollama_http("/api/tags", timeout=5.0).get("models", []):
            caps = ollama_capabilities(m["name"])
            if caps is None or "completion" in caps:          # leave embedding-only models out
                out.append({"id": m["name"], "vision": None if caps is None else "vision" in caps})
        return out
    raise ValueError(f"unknown provider {provider!r}")


def _configured(provider):
    if provider == "anthropic":
        return bool(config._env("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY"
    if provider == "openai":
        return bool(config.OPENAI_API_KEY), "OPENAI_API_KEY"
    if provider == "gemini":
        return bool(config.GEMINI_API_KEY), "GEMINI_API_KEY or GOOGLE_API_KEY"
    if provider == "ollama":
        return True, "OLLAMA_BASE_URL"
    if provider == "claude_code":
        return os.path.isfile(config.CLAUDE_CODE_BIN), "CLAUDE_CODE_BIN (the `claude` CLI, logged in)"
    return True, "LOCAL_LLM_BASE_URL"


def _redact_url(url):
    """scheme://host:port/path: no user:pass@, query string or fragment (they can carry secrets)."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url or "")
        host = u.hostname or ""
        host = f"[{host}]" if ":" in host else host            # IPv6 literal
        if not (u.scheme and host):
            return None if not url else "(invalid URL)"
        return f"{u.scheme}://{host}{':%d' % u.port if u.port else ''}{u.path}"
    except ValueError:
        return "(invalid URL)"


def unavailable_reason(provider):
    """Why a provider cannot run as configured (names an env variable, never its value), or None. No network."""
    p = next((x for x in providers() if x["id"] == provider), None)
    if p is None:
        return f"unknown provider {provider!r}"
    if p["missing_sdk"]:
        return f"Python package missing: {p['missing_sdk']}"
    if not p["configured"]:
        return ("the claude CLI was not found (install Claude Code and log in, or set CLAUDE_CODE_BIN)"
                if provider == "claude_code" else f"{p['env']} is not set")
    return None


def providers():
    """Every provider with whether it is set up. No network calls."""
    out = []
    for pid, label in config.LLM_PROVIDERS.items():
        ok, env = _configured(pid)
        pip = sdk_missing(pid)
        out.append({"id": pid, "label": label, "configured": ok and not pip, "env": env,
                    "missing_sdk": pip, "default_model": config.DEFAULT_MODELS.get(pid),
                    "base_url": _redact_url({"ollama": config.OLLAMA_BASE_URL,
                                             "openai_compatible": config.LOCAL_LLM_BASE_URL}.get(pid))})
    return out


def list_models(provider):
    """The models a provider offers right now. Errors come back as a message, not an exception,
    so one unreachable provider does not break the picker."""
    if provider not in config.LLM_PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    info = next(p for p in providers() if p["id"] == provider)
    if info["missing_sdk"]:
        return dict(info, models=[], error=f"install it: .venv/bin/pip install {info['missing_sdk']}")
    if not info["configured"]:
        return dict(info, models=[], error=f"set {info['env']} in full-workflow-api/.env")
    try:
        return dict(info, models=_models(provider), error=None)
    except Exception as e:                       # unreachable server, bad key...
        msg = f"{type(e).__name__}: {e}"
        if provider == "openai_compatible" and type(e).__name__ == "APIConnectionError":
            msg = f"no server at {config.LOCAL_LLM_BASE_URL} (start LM Studio's server, or set LOCAL_LLM_BASE_URL)"
        return dict(info, models=[], error=msg)
