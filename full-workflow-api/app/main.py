"""FastAPI app: the pipeline as an API, plus the review-card UI at /.

Run:  uvicorn app.main:app --reload   (from full-workflow-api/)
"""

import json
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import collection, config, dataset, evaluate, export, fetch, genmedia, ingest, iterations, jobs, llm, pipeline, recap, scoring
from . import prompt_versions as pv
from .config import STATIC

app = FastAPI(title="PartGram scale-worthiness review", version="1.0")

Verdict = Literal["SCALE", "DO_NOT_SCALE", "DEFER_TO_HUMAN", "FLAG_AUTHENTICITY"]


class Decision(BaseModel):
    action: Literal["scale", "do_not_scale", "override"]
    override_verdict: Optional[Verdict] = None      # required when action == "override"
    note: str = ""
    reviewer: str = "anonymous"
    mode: Literal["card", "text"] = "card"          # which view the reviewer decided from
    elapsed_ms: Optional[int] = Field(default=None, ge=0)


class GroundTruthRows(BaseModel):
    rows: list[dict[str, Optional[str | int | float]]]   # one per video: video_id + the result columns


class ManualReviewRows(BaseModel):
    reviewer: str
    rows: list[dict[str, Optional[str | int | float]]]   # one per video: video_id + decision, reason, minutes_spent


class VideoUrl(BaseModel):
    url: str = ""                                   # blank clears it


class StartJob(BaseModel):
    force: bool = False                             # re-run videos that are already scored
    urls: dict[str, str] = Field(default_factory=dict)   # video_id -> URL, saved before the run


class PromptVersion(BaseModel):
    parent: str = "P6"                              # the version this one is derived from
    text: str                                       # the full template; {video_summary} exactly once
    note: str = ""                                  # what changed and why (from the error report)


class Adopt(BaseModel):
    id: str
    override_reason: str = ""                       # required when the version has not beaten P5


class StartIteration(BaseModel):
    prompt_id: str
    force: bool = False                             # re-score even when an answer for this exact prompt is cached
    note: str = ""


class NewCollection(BaseModel):
    source: str
    name: str = Field(min_length=1, max_length=100)
    children: int = Field(0, ge=0, le=collection.MAX_CHILDREN)


class MoreChildren(BaseModel):
    n: int = Field(ge=1, le=collection.MAX_CHILDREN)


class DatasetForm(BaseModel):
    tables: dict[str, list[dict[str, Optional[str | int | float]]]]   # file name -> rows from the form
    name: Optional[str] = None                      # the session's name


def _check(video_id):
    if video_id not in ingest.load_inputs()[0]:
        raise HTTPException(404, f"unknown video_id {video_id!r}")


def _set_url(video_id, url):
    _check(video_id)
    url = url.strip()
    if url:
        try:
            fetch.check_url(url)
        except ValueError as e:
            raise HTTPException(422, f"{video_id}: {e}") from e
    if url != (ingest.video_url(video_id) or ""):
        ingest.set_video_url(video_id, url)


def _not_while_running():
    if jobs.running():
        raise HTTPException(409, "the pipeline is running; wait for it to finish")


@app.get("/api/dataset")
def get_dataset():
    return dataset.describe()


@app.get("/api/dataset/schema")
def get_schema():
    """The form's tables and columns, mirroring data-template/ (answer keys excluded)."""
    return dataset.schema()


@app.post("/api/dataset")
def create_dataset(body: DatasetForm):
    """Build a dataset from the form rows. It becomes the active dataset, with a fresh runs/ folder."""
    _not_while_running()
    try:
        return dataset.create(body.tables, body.name)
    except dataset.DatasetError as e:
        raise HTTPException(422, {"message": str(e), "errors": e.errors}) from e


@app.post("/api/dataset/reset")
def reset_dataset():
    """New session: close the open one (it stays saved) and start from an empty form."""
    _not_while_running()
    return dataset.reset()


@app.get("/api/sessions")
def list_sessions():
    """Saved sessions, most recently changed first."""
    return dataset.list_sessions()


@app.get("/api/collections")
def list_collections():
    """Parent collections of sessions (runs of one source session), newest first, with a summary each."""
    return collection.listing()


@app.post("/api/collections")
def create_collection(body: NewCollection):
    """New collection of a saved session, with `children` copies of it ("<name> · run 001", ...).
    The source is only read. Copies are APFS clones on macOS, plain copies elsewhere."""
    _not_while_running()
    try:
        return collection.create(body.source, body.name, body.children)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/collections/{cid}")
def get_collection(cid: str):
    """One collection: its runs' session ids and, once a batch has run, its test summary and report."""
    try:
        return collection.get(cid)
    except KeyError as e:
        raise HTTPException(404, f"unknown collection {cid!r}") from e


@app.post("/api/collections/{cid}/children")
def add_collection_children(cid: str, body: MoreChildren):
    """Add n more runs to a collection."""
    _not_while_running()
    try:
        collection.add_children(cid, body.n)
        return collection.get(cid)
    except KeyError as e:
        raise HTTPException(404, f"unknown collection {cid!r}") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/session")
def get_session():
    """The open session's memory (name, timestamps, form rows), or null."""
    return dataset.current_session()


@app.api_route("/api/session", methods=["PUT", "POST"])
def save_session(body: DatasetForm):
    """Autosave the form. Opens a new session when none is open. POST is for sendBeacon on page close."""
    if dataset.current_session() is None:
        _not_while_running()
    return dataset.save_draft(body.tables, body.name)


@app.post("/api/sessions/{session_id}/copy")
def copy_session(session_id: str):
    """Duplicate a saved session, results included. The open session stays open."""
    _not_while_running()
    try:
        return dataset.copy_session(session_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a saved session and everything in its folder. Closes it if it is open."""
    _not_while_running()
    try:
        return dataset.delete_session(session_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/sessions/{session_id}/export")
def export_session(session_id: str, media: bool = False):
    """Download a session's results as a ZIP: results.csv (one row per video), the decision log,
    every stage's JSON, the dataset, and - once locked - ground truth and the evaluation report.
    media=true also packs clips, audio, keyframes and recap videos (can be large)."""
    try:
        path, name = export.build(session_id, media=media)
    except export.ExportError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get("/api/sessions/{session_id}/prompt-pack", response_class=PlainTextResponse)
def prompt_pack(session_id: str):
    """One Markdown file to hand another AI for tuning P6: the brief and constraints, the current P6,
    the evaluation, and per video the exact input, the model's answer, human decisions and the real
    CAC outcome (misses first). The session must be open; outcomes appear once ground truth is locked."""
    try:
        text, name = export.prompt_pack(session_id)
    except export.NotOpen as e:
        raise HTTPException(409, str(e)) from e
    except export.ExportError as e:
        raise HTTPException(404, str(e)) from e
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/sessions/{session_id}/open")
def open_session(session_id: str):
    _not_while_running()
    try:
        return dataset.open_session(session_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


def _read_upload(f, limit):
    data = f.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{f.filename} is larger than {limit // 2**20} MB")
    return f.filename, data


def _gt(call):
    try:
        return call()
    except dataset.GroundTruthLocked as e:
        raise HTTPException(409, str(e)) from e
    except dataset.DatasetError as e:
        raise HTTPException(422, {"message": str(e), "errors": e.errors}) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/ground-truth")
def get_ground_truth():
    """The open campaign's real results per video: draft, or locked with its SHA-256."""
    return _gt(dataset.ground_truth)


@app.put("/api/ground-truth")
def save_ground_truth(body: GroundTruthRows):
    """Autosave the draft. Refused (409) once locked."""
    return _gt(lambda: dataset.save_ground_truth_draft(body.rows))


@app.post("/api/ground-truth/lock")
def lock_ground_truth(body: GroundTruthRows):
    """Write ground_truth.csv and lock it for good. Evaluation measures against it."""
    return _gt(lambda: dataset.lock_ground_truth(body.rows))


@app.get("/api/manual-review")
def get_manual_review(reviewer: str):
    """One reviewer's blind sheet: each video's videos.csv signals (no AI output) and their decisions."""
    return _gt(lambda: dataset.manual_review(reviewer))


@app.get("/api/reviews")
def get_reviews():
    """Every reviewer of the open campaign, with their submitted decisions per video."""
    return _gt(dataset.reviews)


@app.delete("/api/manual-review")
def delete_manual_review(reviewer: str):
    """Delete one reviewer's sheet (draft or submitted); manual_review.csv is rewritten without it."""
    return _gt(lambda: dataset.delete_manual_review(reviewer))


@app.put("/api/manual-review")
def save_manual_review(body: ManualReviewRows):
    """Autosave the reviewer's draft. Refused (409) once submitted."""
    return _gt(lambda: dataset.save_manual_review_draft(body.reviewer, body.rows))


@app.post("/api/manual-review/submit")
def submit_manual_review(body: ManualReviewRows):
    """Freeze the reviewer's sheet and rewrite manual_review.csv. Evaluation compares it with the AI."""
    return _gt(lambda: dataset.submit_manual_review(body.reviewer, body.rows))


@app.post("/api/dataset/answer-keys")
def upload_answer_keys(files: list[UploadFile] = File(...)):
    """ground_truth.csv and/or ground_truth_media.csv for the Evaluation tab."""
    try:
        return dataset.save_answer_keys([_read_upload(f, dataset.MAX_FILE_BYTES) for f in files])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/videos/{video_id}/clip")
def upload_clip(video_id: str, file: UploadFile = File(...)):
    """Upload the video file itself, instead of giving a URL. Re-scores the video on the next run."""
    _check(video_id)
    _not_while_running()
    try:
        dataset.save_clip(video_id, file.file)
    except ValueError as e:
        raise HTTPException(422, f"{video_id}: {e}") from e
    pipeline.invalidate(video_id)
    return pipeline.status(video_id)


@app.post("/api/video-files")
def upload_video_file(file: UploadFile = File(...)):
    """Upload a video to this machine instead of linking it. Returns a file:// URL for the
    form's Video URL column; the pipeline reads the file from there like any other URL."""
    try:
        return {"url": dataset.save_video_file(file.file, file.filename)}
    except ValueError as e:
        raise HTTPException(422, f"{file.filename}: {e}") from e


@app.put("/api/videos/{video_id}/url")
def set_url(video_id: str, body: VideoUrl):
    """The URL this video is downloaded from when the pipeline runs."""
    _set_url(video_id, body.url)
    return pipeline.status(video_id)


@app.post("/api/pipeline/start")
def start_pipeline(body: StartJob):
    """Run every video of the active dataset in the background. Poll /api/pipeline/status."""
    for vid, url in body.urls.items():
        _set_url(vid, url)
    if not jobs.start(list(ingest.load_inputs()[0]), force=body.force):
        raise HTTPException(409, "the pipeline is already running")
    return jobs.snapshot()


@app.get("/api/pipeline/status")
def pipeline_status():
    return jobs.snapshot()


@app.get("/api/health")
def health():
    return {"ok": True, "llm": config.llm_choice(), "model": config.llm_choice()["model"], "videos": len(ingest.load_inputs()[0]),
            "tts": genmedia.tts_engine(), "image_generation": genmedia.available()}


@app.get("/api/videos")
def list_videos():
    """Step 1 - what was ingested, and how far each video is through the pipeline."""
    decided = {d["video_id"] for d in evaluate.read_decisions()}
    out = []
    for vid, row in ingest.load_inputs()[0].items():
        s = pipeline.load(vid, pipeline.active_stage()) or {}
        out.append({"video_id": vid, "brand": row.get("brand"), "creator": row.get("handle"),
                    "caption": row.get("caption"), "verdict": s.get("verdict"),
                    "score": s.get("score"), "decided": vid in decided,
                    **pipeline.status(vid)})
    return out


@app.post("/api/videos/{video_id}/process")
def process(video_id: str, force: bool = False):
    """Steps 2-7 for one video: preprocess, visual analysis, merge, P6, rule guard."""
    _check(video_id)
    try:
        return pipeline.process(video_id, force=force)
    except Exception as e:                                   # surface the cause to the UI
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e


@app.post("/api/process-all")
def process_all(force: bool = False):
    results = {}
    for vid in ingest.load_inputs()[0]:
        try:
            results[vid] = pipeline.process(vid, force=force)["score"]["verdict"]
        except Exception as e:
            results[vid] = f"error: {type(e).__name__}: {e}"
    return results


@app.get("/api/videos/{video_id}/card")
def get_card(video_id: str):
    """Step 8 - the review card payload."""
    _check(video_id)
    return pipeline.card(video_id)


@app.get("/api/videos/{video_id}/summary", response_class=PlainTextResponse)
def get_summary(video_id: str, multimodal: bool = True):
    """Step 4 - the flatten() text: 6 sections, or 7 with CONTENT FEATURES."""
    _check(video_id)
    return pipeline.summary(video_id, multimodal)


def _recap_payload(campaign_id):
    """A recap's metadata plus the URLs of its files, or None when it has not been generated."""
    meta = recap.load(campaign_id)
    mp4 = recap.folder(campaign_id) / "recap.mp4"
    if not meta or not mp4.exists():
        return None
    base, v = f"/artifacts/recaps/{campaign_id}", int(mp4.stat().st_mtime)
    return dict(meta, video_url=f"{base}/recap.mp4?v={v}", captions_url=f"{base}/recap.vtt?v={v}",
                chart_url=f"{base}/chart.png?v={v}")


def _check_campaign(campaign_id):
    # the id becomes a folder name, and uploaded datasets choose their own ids
    if not recap.campaign_videos(campaign_id) or not config.DATASET_ID_RE.match(campaign_id):
        raise HTTPException(404, f"unknown campaign_id {campaign_id!r}")


@app.get("/api/campaigns")
def list_campaigns():
    """Step 10 - campaigns, how many of their videos are scored, and their recap if generated."""
    out = {}
    for vid, row in ingest.load_inputs()[0].items():
        cid = row.get("campaign_id")
        if cid is None:
            continue
        c = out.setdefault(cid, {"campaign_id": cid, "brand": row.get("brand"),
                                 "brand_category": row.get("brand_category"), "videos": 0, "scored": 0})
        c["videos"] += 1
        c["scored"] += int(pipeline.status(vid)["scored"])
    for cid, c in out.items():
        c["recap"] = _recap_payload(cid) if config.DATASET_ID_RE.match(cid) else None
    return list(out.values())


@app.post("/api/campaigns/{campaign_id}/recap")
def make_recap(campaign_id: str, force: bool = False, language: Optional[Literal["en", "es"]] = None):
    """Step 10 - build the ~45 s recap video for a campaign (script, voice, overlays, captions).
    Without `language` it is narrated in the campaign's own language, in the videos' voice."""
    _check_campaign(campaign_id)
    try:
        recap.build(campaign_id, language=language, force=force)
    except recap.NotReady as e:
        raise HTTPException(409, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e
    return _recap_payload(campaign_id)


@app.get("/api/campaigns/{campaign_id}/recap")
def get_recap(campaign_id: str):
    _check_campaign(campaign_id)
    payload = _recap_payload(campaign_id)
    if payload is None:
        raise HTTPException(404, "no recap generated for this campaign yet")
    return payload


@app.get("/api/videos/{video_id}/clip")
def clip(video_id: str):
    _check(video_id)
    p = ingest.clip_path(video_id)
    if p is None:
        raise HTTPException(404, "no clip for this video")
    return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "no-cache"})


@app.get("/api/videos/{video_id}/captions.vtt")
def captions(video_id: str):
    _check(video_id)
    return PlainTextResponse(pipeline.captions_vtt(video_id), media_type="text/vtt")


@app.post("/api/videos/{video_id}/decision")
def decide(video_id: str, body: Decision):
    """Step 9 - the human decision, appended to runs/decisions.jsonl."""
    _check(video_id)
    if body.action == "override" and not body.override_verdict:
        raise HTTPException(422, "override needs override_verdict")
    if body.action == "override" and not body.note.strip():
        raise HTTPException(422, "override needs a note explaining why")

    s = pipeline.load(video_id, pipeline.active_stage()) or {}
    human = {"scale": "SCALE", "do_not_scale": "DO_NOT_SCALE"}.get(body.action, body.override_verdict)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video_id": video_id,
        "reviewer": body.reviewer.strip() or "anonymous",
        "action": body.action,
        "human_verdict": human,
        "ai_verdict": s.get("verdict"),
        "ai_model_verdict": s.get("model_verdict"),
        "ai_score": s.get("score"),
        "ai_prompt": s.get("prompt_id"),
        "agrees_with_ai": human == s.get("verdict"),
        "note": body.note.strip(),
        "mode": body.mode,
        "elapsed_ms": body.elapsed_ms,
    }
    with config.DECISIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


@app.get("/api/decisions")
def decisions():
    return evaluate.read_decisions()


@app.get("/api/thresholds")
def get_thresholds():
    """The rule guard thresholds of the open session, and their defaults."""
    return dataset.get_thresholds()


@app.put("/api/thresholds")
def put_thresholds(body: dict):
    """Change the rule guard thresholds; cached P5/P6 verdicts follow at once, no model call."""
    try:
        return dataset.set_thresholds(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/llm")
def get_llm():
    """The provider + model the open session runs on, the env default, and which providers are set up."""
    return dataset.get_llm()


@app.get("/api/llm/models")
def llm_models(provider: str):
    """The models one provider offers now (Anthropic / OpenAI / Gemini list calls are free; local
    servers are asked directly). vision: true, false, or null when the provider does not say."""
    try:
        return llm.list_models(provider)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.put("/api/llm")
def put_llm(body: dict):
    """Choose the provider + model for the open session. Cached answers of each model are kept apart,
    so the Review and Evaluation tabs show the chosen model's results."""
    _not_while_running()
    try:
        return dataset.set_llm(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/evaluate")
def get_evaluation(run_missing: bool = False, reviewer: Optional[list[str]] = Query(None)):
    """P5 vs P6 vs the blind manual review against ground truth, feature accuracy, reviewer timing.
    run_missing=true scores any video that has not been processed yet (API calls).
    reviewer (repeatable) limits the human side to those reviewers; omitted = everyone."""
    try:
        return evaluate.run(run_missing=run_missing, reviewers=reviewer)
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e


# --- Prompt versions and iterations (the Section 6 tuning loop) -----------------------------------

def _pv(call):
    try:
        return call()
    except KeyError as e:
        raise HTTPException(404, f"unknown {e.args[0]!r}") from e
    except pv.Conflict as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/prompts")
def list_prompts():
    """Every prompt version (P5 baseline, built-in P6, P6.2, ...), the active one and the adoption log."""
    return _pv(pv.listing)


@app.get("/api/prompts/{prompt_id}")
def get_prompt(prompt_id: str):
    return _pv(lambda: pv.get(prompt_id))


@app.get("/api/prompts/{prompt_id}/diff", response_class=PlainTextResponse)
def diff_prompt(prompt_id: str, against: Optional[str] = None):
    """Unified diff of a version against its parent, or against ?against=<id>."""
    return _pv(lambda: pv.diff(prompt_id, against))


@app.post("/api/prompts", status_code=201)
def create_prompt(body: PromptVersion):
    """Save a new, immutable version derived from body.parent."""
    _not_while_running()
    return _pv(lambda: pv.create(body.parent, body.text, body.note))


@app.delete("/api/prompts/{prompt_id}")
def delete_prompt(prompt_id: str):
    _not_while_running()
    _pv(lambda: pv.delete(prompt_id))
    return pv.listing()


@app.put("/api/prompts/active")
def adopt_prompt(body: Adopt):
    """Adopt a version for the review card. Refused (409) unless its latest iteration on the current
    model beats P5, or a written override reason is given (logged). P6 can always be restored."""
    _not_while_running()

    def go():
        check = None if body.id in (pv.DEFAULT_ACTIVE, pv.BASELINE) else iterations.adoption_check(body.id)
        return pv.set_active(body.id, check, body.override_reason, (check or {}).get("iteration"))
    return _pv(go)


@app.get("/api/prompts/{prompt_id}/adoption")
def adoption_check(prompt_id: str):
    """What the gate would say for this version right now, without adopting it."""
    return _pv(lambda: iterations.adoption_check(prompt_id))


@app.get("/api/iterations")
def list_iterations():
    """Every iteration, newest first, each compared with P5 against the ground truth."""
    return _pv(iterations.listing)


@app.get("/api/iterations/{n}")
def get_iteration(n: int):
    return _pv(lambda: iterations.get(n))


@app.post("/api/iterations")
def start_iteration(body: StartIteration):
    """Score every video with body.prompt_id and with P5 in the background (poll /api/pipeline/status),
    then freeze the verdicts as a new iteration."""
    started = _pv(lambda: iterations.start(body.prompt_id, force=body.force, note=body.note))
    if started is False:
        raise HTTPException(409, "the pipeline is already running")
    return started


@app.get("/api/verdicts")
def verdicts():
    return scoring.VERDICTS


@app.get("/artifacts/{path:path}")
def artifact(path: str):
    """Pipeline artifacts (keyframes) of the active dataset."""
    if config.RUNS is None:
        raise HTTPException(404, "not found")
    root = config.RUNS.resolve()
    p = (root / path).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})     # revalidate, see _frame_url


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
