"""FastAPI app: the pipeline as an API, plus the review-card UI at /.

Run:  uvicorn app.main:app --reload   (from full-workflow-api/)
"""

import json
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, dataset, evaluate, fetch, genmedia, ingest, jobs, pipeline, recap, scoring
from .config import MODEL, STATIC

app = FastAPI(title="PartGram scale-worthiness review", version="1.0")

Verdict = Literal["SCALE", "DO_NOT_SCALE", "DEFER_TO_HUMAN", "FLAG_AUTHENTICITY"]


class Decision(BaseModel):
    action: Literal["scale", "do_not_scale", "override"]
    override_verdict: Optional[Verdict] = None      # required when action == "override"
    note: str = ""
    reviewer: str = "anonymous"
    mode: Literal["card", "text"] = "card"          # which view the reviewer decided from
    elapsed_ms: Optional[int] = Field(default=None, ge=0)


class VideoUrl(BaseModel):
    url: str = ""                                   # blank clears it


class StartJob(BaseModel):
    force: bool = False                             # re-run videos that are already scored
    urls: dict[str, str] = Field(default_factory=dict)   # video_id -> URL, saved before the run


class UseDataset(BaseModel):
    id: Optional[str] = None                        # None = the example dataset


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


@app.post("/api/dataset")
def upload_dataset(files: list[UploadFile] = File(...)):
    """Upload the dataset CSVs. They become the active dataset, with their own runs/ folder."""
    _not_while_running()
    blobs = []
    for f in files:
        data = f.file.read(dataset.MAX_FILE_BYTES + 1)
        if len(data) > dataset.MAX_FILE_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than {dataset.MAX_FILE_BYTES // 2**20} MB")
        blobs.append((f.filename, data))
    try:
        return dataset.save(blobs)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/dataset/use")
def use_dataset(body: UseDataset):
    _not_while_running()
    try:
        return dataset.use(body.id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


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
    return {"ok": True, "model": MODEL, "videos": len(ingest.load_inputs()[0]),
            "tts": genmedia.tts_engine(), "image_generation": genmedia.available()}


@app.get("/api/videos")
def list_videos():
    """Step 1 - what was ingested, and how far each video is through the pipeline."""
    decided = {d["video_id"] for d in evaluate.read_decisions()}
    out = []
    for vid, row in ingest.load_inputs()[0].items():
        s = pipeline.load(vid, "score_p6") or {}
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

    s = pipeline.load(video_id, "score_p6") or {}
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


@app.get("/api/evaluate")
def get_evaluation(run_missing: bool = False):
    """P5 vs P6 against ground truth, feature accuracy, reviewer timing.
    run_missing=true scores any video that has not been processed yet (API calls)."""
    try:
        return evaluate.run(run_missing=run_missing)
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e


@app.get("/api/verdicts")
def verdicts():
    return scoring.VERDICTS


@app.get("/artifacts/{path:path}")
def artifact(path: str):
    """Pipeline artifacts (keyframes) of the active dataset."""
    root = config.RUNS.resolve()
    p = (root / path).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})     # revalidate, see _frame_url


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
