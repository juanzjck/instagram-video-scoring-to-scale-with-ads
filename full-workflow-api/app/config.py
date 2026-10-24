"""Paths, environment and the thresholds the rule guard enforces."""

import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Local .env wins; the first-test project's .env is the fallback so the key lives in one place.
load_dotenv(ROOT / ".env")
_FALLBACK = dotenv_values(ROOT.parent / "FirstTest-prompts" / ".env")


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


MODEL = _env("LLM_MODEL") or "claude-opus-5-5"
WORKSPACE_ID = _env("ANTHROPIC_WORKSPACE_ID")
WHISPER_MODEL = _env("WHISPER_MODEL") or "base"

# Generated media (app/genmedia.py). Optional: without a key the voice is macOS `say`.
OPENAI_API_KEY = _env("OPENAI_API_KEY")
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


def thresholds():
    """The open session's rule guard thresholds: the defaults above unless changed in session.json."""
    t = dict(DEFAULT_THRESHOLDS)
    p = DATA / "session.json" if DATA is not None else None
    if p is not None and p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8")).get("thresholds") or {}
        except ValueError:
            saved = {}
        t.update({k: saved[k] for k in t if isinstance(saved.get(k), (int, float))})
    return t
CORE_METRICS = ["reach", "views", "total_interactions", "saved", "shares"]

COMMENT_LIMIT = 10
FRAME_HEIGHT = 768      # keyframes are downscaled before being sent to the vision model
