"""Session export (app/export.py): offline, on a throwaway uploads/ folder."""

import csv
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config, export
from app.main import app

SID = "20260101-000000-000000"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    monkeypatch.setattr(config, "DATA", None)
    f = tmp_path / SID
    (f / "runs" / "v1" / "frames").mkdir(parents=True)
    (f / "runs" / "v2").mkdir(parents=True)
    (f / "session.json").write_text(json.dumps({"id": SID, "name": "Acme · c1", "ground_truth": {"locked_at": None}}))
    (f / "videos.csv").write_text("video_id,creator_id,campaign_id,hours_since_post,reach\nv1,cr1,c1,120,40000\nv2,cr1,c1,,\n")
    (f / "creators.csv").write_text("creator_id,handle\ncr1,@ana\n")
    (f / "campaigns.csv").write_text("campaign_id,brand\nc1,Acme\n")
    (f / "ground_truth.csv").write_text("video_id,expected_verdict\nv1,SCALE\n")
    (f / "runs" / "v1" / "score_p6.json").write_text(json.dumps(
        {"verdict": "SCALE", "model_verdict": "SCALE", "score": 74, "confidence": "MEDIUM",
         "rule_hits": [], "rule_override": False, "reasons": ["a", "b"]}))
    (f / "runs" / "v1" / "clip.mp4").write_bytes(b"\0" * 10)
    (f / "runs" / "v1" / "frames" / "kf_00.jpg").write_bytes(b"\0")
    (f / "runs" / "decisions.jsonl").write_text(
        json.dumps({"ts": "t1", "video_id": "v1", "action": "scale", "human_verdict": "SCALE", "reviewer": "jp"}) + "\n")
    return f


def _zip(session_id, media=False):
    path, name = export.build(session_id, media=media)
    try:
        with zipfile.ZipFile(path) as z:
            return name, {n: z.read(n) for n in z.namelist()}
    finally:
        path.unlink()


def test_results_csv_has_one_row_per_video_and_keeps_blanks(session):
    _, files = _zip(SID)
    rows = list(csv.DictReader(io.StringIO(files["results.csv"].decode())))
    assert [r["video_id"] for r in rows] == ["v1", "v2"]
    v1, v2 = rows
    assert (v1["p6_verdict"], v1["p6_score"], v1["brand"], v1["creator_handle"]) == ("SCALE", "74", "Acme", "@ana")
    assert (v1["human_verdict"], v1["reviewer"], v1["decisions_logged"]) == ("SCALE", "jp", "1")
    assert v1["p6_reasons"] == "a | b"
    assert v2["p6_verdict"] == "" and v2["reach"] == "" and v2["p6_score"] == ""   # blank, never 0


def test_media_and_unlocked_answer_keys_are_left_out(session):
    _, files = _zip(SID)
    assert "runs/v1/score_p6.json" in files and "runs/decisions.jsonl" in files
    assert "runs/v1/clip.mp4" not in files and "runs/v1/frames/kf_00.jpg" not in files
    assert "ground_truth.csv" not in files
    assert "not locked" in files["MANIFEST.txt"].decode()


def test_media_flag_and_locked_ground_truth_are_included(session):
    (session / "session.json").write_text(json.dumps({"id": SID, "ground_truth": {"locked_at": "2026-01-02T00:00:00+00:00"}}))
    name, files = _zip(SID, media=True)
    assert name.endswith("-media.zip")
    assert "runs/v1/clip.mp4" in files and "runs/v1/frames/kf_00.jpg" in files
    assert "ground_truth.csv" in files


@pytest.mark.parametrize("bad", ["../etc", "nope", ""])
def test_unknown_session_is_rejected(session, bad):
    with pytest.raises(export.ExportError):
        export.build(bad)


def test_export_route_returns_a_zip(session):
    r = TestClient(app).get(f"/api/sessions/{SID}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert "results.csv" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert TestClient(app).get("/api/sessions/missing/export").status_code == 404


def test_prompt_pack_needs_the_session_open(session):
    with pytest.raises(export.NotOpen):
        export.prompt_pack(SID)
    assert TestClient(app).get(f"/api/sessions/{SID}/prompt-pack").status_code == 409


def test_prompt_pack_has_brief_prompt_input_and_output(session, monkeypatch):
    from app import ingest
    p6 = json.loads((session / "runs" / "v1" / "score_p6.json").read_text())
    p6["raw_output"] = "VERDICT: SCALE\nSCORE: 74\nSCORE_RANGE: 66-82\nCONFIDENCE: MEDIUM\nREASONS:\n1. a\n2. b"
    (session / "runs" / "v1" / "score_p6.json").write_text(json.dumps(p6))
    config.activate(SID)
    ingest.load_inputs.cache_clear() if hasattr(ingest.load_inputs, "cache_clear") else None
    try:
        text, name = export.prompt_pack(SID)
    finally:
        config.activate(None)
    assert name.endswith(".md")
    assert "## Constraints" in text and "{video_summary}" in text      # the brief and the raw P6 template
    assert "### v1" in text and "VERDICT: SCALE" in text                # the scored video and its answer
    assert "### v2" not in text                                         # not scored, nothing to learn from
    assert "reviewer jp decided SCALE" in text
    assert "NOT locked" in text
