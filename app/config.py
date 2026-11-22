"""Paths, environment and the thresholds the rule guard enforces."""

import json
import os
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Local .env wins; the first-test project's .env is the fallback so the key lives in one place.
load_dotenv(ROOT / ".env")
_FALLBACK = dotenv_values(ROOT / "old" / "FirstTest-prompts" / ".env")


def _env(name):
    """A setting from the environment, else from the fallback .env.

    Blank counts as unset, and so does a value that is only a comment: dotenv reads
    `KEY=    # note` as the string "# note", which is what a copied .env.example contains.
    """
    for v in (os.environ.get(name), _FALLBACK.get(name)):
        v = (v or "").strip()
        if v and not v.startswith("#"):
            return v
    return None


if _env("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = _env("ANTHROPIC_API_KEY")   # the SDK reads it from here
else:
    os.environ.pop("ANTHROPIC_API_KEY", None)

STATIC = ROOT / "app" / "static"
UPLOADS = ROOT / "uploads"                  # one folder per dataset built in the Dataset form
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# The active dataset. Set by activate(); read these as config.DATA etc. so a dataset
# switch is seen everywhere (do not `from .config import DATA`). Every server start is a
# clean session: no dataset until the user builds one in the form.
DATASET_ID = None
DATA = MEDIA = RUNS = DECISIONS_LOG = None


def activate(dataset_id=None):
    """Point the app at a dataset under uploads/, or at no dataset when None.

    Each dataset keeps its own runs/ (pipeline cache and decision log), so datasets that
    reuse video ids never read each other's results.
    """
    global DATASET_ID, DATA, MEDIA, RUNS, DECISIONS_LOG
    if dataset_id is None:
        DATASET_ID = DATA = MEDIA = RUNS = DECISIONS_LOG = None
        return
    if not DATASET_ID_RE.match(dataset_id) or not (UPLOADS / dataset_id).is_dir():
        raise ValueError(f"unknown dataset {dataset_id!r}")
    data = (UPLOADS / dataset_id).resolve()
    runs = data / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    DATASET_ID, DATA, MEDIA, RUNS = dataset_id, data, data / "media", runs
    DECISIONS_LOG = runs / "decisions.jsonl"


# Which LLM runs the pipeline (app/llm.py). LLM_PROVIDER picks the default provider and
# LLM_MODEL its model; a session can override both from the UI (session.json "llm").
LLM_PROVIDERS = {
    "anthropic": "Anthropic Claude",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "ollama": "Ollama (local)",
    "openai_compatible": "Local OpenAI-compatible server (LM Studio, llama.cpp, vLLM)",
    "claude_code": "Claude Code CLI (claude -p, subscription login)",
}
LLM_PROVIDER = (_env("LLM_PROVIDER") or "anthropic").lower()
if LLM_PROVIDER not in LLM_PROVIDERS:
    raise ValueError(f"LLM_PROVIDER={LLM_PROVIDER!r}: use one of {', '.join(LLM_PROVIDERS)}")
# Per-provider default model. None = pick one in the UI (the list comes from the provider).
DEFAULT_MODELS = {
    "anthropic": _env("ANTHROPIC_MODEL") or "claude-opus-5-5",
    "openai": _env("OPENAI_MODEL"),
    "gemini": _env("GEMINI_MODEL"),
    "ollama": _env("OLLAMA_MODEL"),
    "openai_compatible": _env("LOCAL_LLM_MODEL"),
    "claude_code": _env("CLAUDE_CODE_MODEL") or "claude-opus-5-5",
}
if _env("LLM_MODEL"):
    DEFAULT_MODELS[LLM_PROVIDER] = _env("LLM_MODEL")
MODEL = DEFAULT_MODELS["anthropic"]          # the Claude model when Anthropic is the provider
WORKSPACE_ID = _env("ANTHROPIC_WORKSPACE_ID")
WHISPER_MODEL = _env("WHISPER_MODEL") or "base"

OPENAI_API_KEY = _env("OPENAI_API_KEY")      # OpenAI LLM, and the recap's TTS / images below
GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
OLLAMA_BASE_URL = (_env("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
# Ollama's own default context is small and it truncates long prompts silently, so ask for room.
OLLAMA_NUM_CTX = int(_env("OLLAMA_NUM_CTX") or 16384)
LOCAL_LLM_BASE_URL = (_env("LOCAL_LLM_BASE_URL") or "http://localhost:1234/v1").rstrip("/")
LOCAL_LLM_API_KEY = _env("LOCAL_LLM_API_KEY") or "not-needed"   # most local servers ignore it
# Claude Code CLI provider (app/llm.py _claude_code): runs `claude -p` headless on the user's own login.
CLAUDE_CODE_BIN = _env("CLAUDE_CODE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
CLAUDE_CODE_TIMEOUT_S = float(_env("CLAUDE_CODE_TIMEOUT_S") or 600)    # per call
CLAUDE_CODE_RETRIES = int(_env("CLAUDE_CODE_RETRIES") or 4)            # on rate limits / overload only
CLAUDE_CODE_MAX_WAIT_S = float(_env("CLAUDE_CODE_MAX_WAIT_S") or 900)  # longer reset = give up, not sleep


def default_llm():
    return {"provider": LLM_PROVIDER, "model": DEFAULT_MODELS.get(LLM_PROVIDER)}


def llm_choice():
    """{"provider", "model"} the open session runs on: session.json's "llm", else the env default."""
    return resolve_llm(_session_value("llm"))


def resolve_llm(saved):
    """A session.json "llm" entry (or None) -> {"provider", "model"}."""
    if isinstance(saved, dict) and saved.get("provider") in LLM_PROVIDERS:
        return {"provider": saved["provider"], "model": saved.get("model") or DEFAULT_MODELS.get(saved["provider"])}
    return default_llm()

# Generated media (app/genmedia.py). Optional: without a key the voice is macOS `say`.
TTS_PROVIDER = (_env("TTS_PROVIDER") or "auto").lower()      # auto | openai | say
TTS_MODEL = _env("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
TTS_VOICE = _env("OPENAI_TTS_VOICE") or "marin"
IMAGE_MODEL = _env("OPENAI_IMAGE_MODEL") or "gpt-image-2"
IMAGE_QUALITY = _env("OPENAI_IMAGE_QUALITY") or "medium"     # low | medium | high

NA = "not available"   # the single rendering of "we have no value" - never 0

# Rule guard thresholds. These are applied in code after the model answers, so no
# amount of good-looking creative can talk a video past them.
MIN_HOURS = 48          # Instagram insights can lag up to 48h
MIN_REACH = 1000        # below this, rate metrics are not reliable
DEFAULT_THRESHOLDS = {"min_hours": MIN_HOURS, "min_reach": MIN_REACH}


def _session_value(key):
    """One key of the open session's session.json, or None."""
    p = DATA / "session.json" if DATA is not None else None
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(key)
    except ValueError:
        return None


def thresholds():
    """The open session's rule guard thresholds: the defaults above unless changed in session.json."""
    t = dict(DEFAULT_THRESHOLDS)
    saved = _session_value("thresholds") or {}
    t.update({k: saved[k] for k in t if isinstance(saved.get(k), (int, float))})
    return t
CORE_METRICS = ["reach", "views", "total_interactions", "saved", "shares"]

COMMENT_LIMIT = 10
FRAME_HEIGHT = 768      # keyframes are downscaled before being sent to the vision model
