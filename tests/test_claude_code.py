"""Offline tests for the Claude Code CLI provider (app/llm.py _claude_code), session collections
(app/collection.py) and the collection test harness (scripts/run_collection_test.py).

The `claude` binary is never run: llm.subprocess is replaced by a fake that records the command,
stdin, cwd and environment and answers with stream-json lines shaped like the real CLI's.
"""

import base64
import hashlib
import importlib.util
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import collection, config, dataset, ingest, jobs, llm, pipeline
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
IMG = base64.standard_b64encode(b"\xff\xd8fake-jpeg").decode()
SAMPLING = ("--temperature", "--top-p", "--top_p", "--top-k", "--seed")


def _events(result=None, structured=None, is_error=False, status=None, stop="end_turn", subtype="success",
            limit=None, usage=None):
    ev = [{"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}]
    if limit:
        ev.append({"type": "rate_limit_event", "rate_limit_info": limit})
    r = {"type": "result", "subtype": subtype, "is_error": is_error, "api_error_status": status,
         "result": result, "stop_reason": stop, "total_cost_usd": 0.0123,
         "usage": usage or {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 20,
                            "output_tokens": 40}}
    if structured is not None:
        r["structured_output"] = structured
    ev.append(r)
    return "\n".join(json.dumps(e) for e in ev) + "\n"


class FakeCLI:
    """Stands in for subprocess.run. `replies` is a list of stdout strings (or a function of stdin)."""

    def __init__(self, *replies, returncode=0, stderr=""):
        self.replies, self.returncode, self.stderr, self.calls = list(replies), returncode, stderr, []

    def run(self, cmd, input=None, capture_output=None, text=None, cwd=None, env=None, timeout=None):
        self.calls.append({"cmd": cmd, "input": input, "cwd": cwd, "env": env, "timeout": timeout,
                           "cwd_files": sorted(p.name for p in Path(cwd).iterdir())})
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        out = r(input) if callable(r) else r
        if isinstance(out, BaseException):
            raise out
        return subprocess.CompletedProcess(cmd, self.returncode, out, self.stderr)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", str(binary))
    monkeypatch.setattr(config, "llm_choice", lambda: {"provider": "claude_code", "model": "claude-opus-5-5"})
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    sleeps = []

    def install(*replies, **kw):
        fake = FakeCLI(*replies, **kw)
        fake.sleeps = sleeps
        monkeypatch.setattr(llm, "subprocess", SimpleNamespace(run=fake.run, TimeoutExpired=subprocess.TimeoutExpired))
        return fake
    return install


# ---------------------------------------------------------------- the CLI call

def test_clean_context_flags_env_and_cwd(cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dead")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    fake = cli(_events("VERDICT: SCALE"))
    out = llm.complete("score this", max_tokens=1200)
    c = fake.calls[0]
    cmd = c["cmd"]
    assert cmd[0] == config.CLAUDE_CODE_BIN and "-p" in cmd
    for flag in ("--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--system-prompt") + 1] == ""          # no system prompt: not Claude Code's agent prompt
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert "--bare" not in cmd                                   # would drop the subscription login
    assert not any(f in cmd for f in SAMPLING) and "--max-tokens" not in cmd
    # no API key (it would override the login), nothing from a parent Claude Code session
    assert "ANTHROPIC_API_KEY" not in c["env"] and not any(k.startswith("CLAUDE") for k in c["env"])
    assert "HOME" in c["env"] and "PATH" in c["env"]
    assert c["cwd_files"] == [] and not Path(c["cwd"]).exists()  # empty temp dir, removed afterwards
    assert not str(c["cwd"]).startswith(str(ROOT.parent))
    msg = json.loads(c["input"])
    assert msg == {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "score this"}]}}
    assert out["text"] == "VERDICT: SCALE" and out["provider"] == "claude_code"
    assert out["input_tokens"] == 123 and out["output_tokens"] == 40 and out["cost_usd"] == 0.0123
    assert llm.usage(out)["cost_usd"] == 0.0123


def test_images_and_json_schema(cli):
    schema = {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}}}
    fake = cli(_events('{"note": "x"}', structured={"note": "a frame"}, stop="tool_use"))
    content = [{"type": "text", "text": "Frame kf00 at t=0s:"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": IMG}}]
    out = llm.complete(content, system="You analyse keyframes.", json_schema=schema)
    cmd = fake.calls[0]["cmd"]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema
    assert cmd[cmd.index("--system-prompt") + 1] == "You analyse keyframes."
    sent = json.loads(fake.calls[0]["input"])["message"]["content"]
    assert sent[1] == content[1]                                 # base64 image block passed through as-is
    assert llm.parse_json(out["text"], schema) == {"note": "a frame"} and out["stop_reason"] == "end_turn"


def test_rate_limit_retried_with_backoff(cli):
    fake = cli(_events("API Error: 429 rate limit", is_error=True, status=429),
               _events("Overloaded", is_error=True, status=529),
               _events("VERDICT: SCALE"))
    assert llm.complete("x")["text"] == "VERDICT: SCALE"
    assert len(fake.calls) == 3 and fake.sleeps == [30, 60]


def test_usage_limit_far_away_raises_at_once(cli):
    later = time.time() + 5 * 3600
    fake = cli(_events("Claude usage limit reached", is_error=True, limit={"status": "rejected", "resetsAt": later}))
    with pytest.raises(llm.RateLimited) as e:
        llm.complete("x")
    assert e.value.resets_at == later and len(fake.calls) == 1 and fake.sleeps == []


def test_rate_limit_gives_up_after_retries(cli, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_CODE_RETRIES", 2)
    fake = cli(_events("rate limit", is_error=True, status=429))
    with pytest.raises(llm.RateLimited):
        llm.complete("x")
    assert len(fake.calls) == 3


@pytest.mark.parametrize("stdout,rc,stderr,exc", [
    (_events(""), 0, "", llm.LLMError),                                           # empty answer
    ("", 1, "boom", llm.LLMError),                                                # no result at all
    ("", 1, "Not logged in · Please run /login", llm.ProviderUnavailable),
    (_events("Not logged in · Please run /login", is_error=True), 1, "", llm.ProviderUnavailable),
    (_events("There's an issue with the selected model", is_error=True, status=404), 1, "", llm.ProviderUnavailable),
    (_events("Something broke", is_error=True, status=400), 1, "", llm.LLMError),
    (_events("partial", subtype="error_during_execution"), 0, "", llm.LLMError),
    (_events("not json, no structured output"), 0, "", None),                    # caught by parse_json below
])
def test_errors_are_raised(cli, stdout, rc, stderr, exc):
    cli(stdout, returncode=rc, stderr=stderr)
    if exc is None:
        out = llm.complete("x", json_schema={"type": "object"})
        with pytest.raises(llm.LLMError):
            llm.parse_json(out["text"], {"type": "object"})
        return
    with pytest.raises(exc):
        llm.complete("x")


def test_refusal_raises(cli):
    cli(_events("I can't help with that.", stop="refusal"))
    with pytest.raises(RuntimeError, match="declined"):
        llm.complete("x")


def test_timeout_raises(cli):
    cli(lambda stdin: subprocess.TimeoutExpired("claude", 1))
    with pytest.raises(llm.LLMError, match="did not answer"):
        llm.complete("x")


def test_missing_binary_is_unavailable(cli, monkeypatch, tmp_path):
    cli(_events("x"))
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", str(tmp_path / "nope"))
    with pytest.raises(llm.ProviderUnavailable, match="not found"):
        llm.complete("x")


def test_listed_as_a_provider(cli):
    p = next(x for x in llm.providers() if x["id"] == "claude_code")
    assert p["configured"] and p["default_model"] == config.DEFAULT_MODELS["claude_code"]
    assert llm.list_models("claude_code")["models"][0]["vision"] is True


# ---------------------------------------------------------------- collections + harness

SRC = "20260101-000000-000000"
VIDEOS = ("v1", "v2", "v3")
WINNERS = {"v1"}


def _csv(path, header, rows):
    path.write_text(",".join(header) + "\n" + "".join(",".join(str(x) for x in r) + "\n" for r in rows))


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    for k in ("UPLOADS", "DATASET_ID", "DATA", "MEDIA", "RUNS", "DECISIONS_LOG"):
        monkeypatch.setattr(config, k, getattr(config, k))
    up = tmp_path / "uploads"
    src = up / SRC
    (src / "runs").mkdir(parents=True)
    monkeypatch.setattr(config, "UPLOADS", up)
    (src / "session.json").write_text(json.dumps({"id": SRC, "name": "XTB · 1", "form": {},
                                                 "ground_truth": {"locked_at": "2026-10-18T00:31:37+00:00"}}))
    _csv(src / "campaigns.csv", ["campaign_id", "brand"], [["1", "XTB"]])
    _csv(src / "creators.csv", ["creator_id", "handle"], [["c1", "@a"]])
    _csv(src / "videos.csv", ["video_id", "creator_id", "campaign_id", "hours_since_post", "reach", "views",
                              "total_interactions", "saved", "shares"],
         [[v, "c1", "1", 200, 50000, 60000, 3000, 500, 200] for v in VIDEOS])
    _csv(src / "ground_truth.csv", ["video_id", "expected_verdict", "performed_well_after_scaling", "cac"],
         [[v, "SCALE" if v in WINNERS else "DO_NOT_SCALE", "yes" if v in WINNERS else "no", 10] for v in VIDEOS])
    for v in VIDEOS:
        (src / "runs" / v).mkdir()
        (src / "runs" / v / "preprocess.json").write_text(json.dumps({"video_id": v, "frames": [], "transcript": []}))
    monkeypatch.setattr(pipeline, "summary", lambda vid, multimodal: f"VID={vid} MM={multimodal} ")
    monkeypatch.setattr(pipeline, "fetch_clip", lambda vid: False)
    monkeypatch.setattr(pipeline, "card", lambda vid, prompt_id=None: {"video_id": vid})
    jobs.reset()
    yield up
    config.activate(None)
    ingest.load_inputs.cache_clear()


def _digest(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_collection_copies_and_groups_without_touching_the_source(uploads):
    before = _digest(uploads / SRC)
    c = collection.create(SRC, "Test 1 - XTB campaign", children=2)
    assert c["children"] == 2 and c["source_name"] == "XTB · 1"
    assert _digest(uploads / SRC) == before                       # the source is only read
    kids = c["child_ids"]
    names = {s["id"]: s for s in dataset.list_sessions()}
    assert [names[k]["name"] for k in kids] == ["Test 1 - XTB campaign · run 001", "Test 1 - XTB campaign · run 002"]
    assert names[kids[1]]["collection"] == {"id": c["id"], "name": "Test 1 - XTB campaign", "index": 2}
    assert names[SRC]["collection"] is None
    assert not any(s["id"].startswith("_") for s in dataset.list_sessions())   # the parent is not a session
    assert (uploads / kids[0] / "ground_truth.csv").read_bytes() == (uploads / SRC / "ground_truth.csv").read_bytes()
    # opening a run and autosaving keeps its run name; copying a run gives a plain session
    dataset.open_session(kids[0])
    assert dataset.save_draft({"campaigns.csv": [{"campaign_id": "1", "brand": "XTB"}]})["name"] == \
        "Test 1 - XTB campaign · run 001"
    plain = dataset.copy_session(kids[0])
    assert dataset._load_session(uploads / plain["id"]).get("collection") is None
    assert collection.find("Test 1 - XTB campaign", SRC)["id"] == c["id"]
    with pytest.raises(ValueError, match="itself a run"):
        collection.create(kids[0], "nested")


def test_collection_api(uploads):
    api = TestClient(app)
    r = api.post("/api/collections", json={"source": SRC, "name": "API test", "children": 1})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert api.post(f"/api/collections/{cid}/children", json={"n": 2}).json()["children"] == 3
    assert [x["id"] for x in api.get("/api/collections").json()] == [cid]
    assert api.get("/api/collections/col-19990101-000000-000000").status_code == 404
    assert api.post("/api/collections", json={"source": "nope", "name": "x"}).status_code == 422


def _harness():
    spec = importlib.util.spec_from_file_location("run_collection_test", ROOT / "scripts" / "run_collection_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scorer(stdin):
    """P6 (multimodal summary) scales exactly the winners; P5 scales everything."""
    text = json.loads(stdin)["message"]["content"][0]["text"]
    vid = text.rsplit("VID=", 1)[1].split()[0]
    p6 = "MM=True" in text
    verdict = ("SCALE" if vid in WINNERS else "DO_NOT_SCALE") if p6 else "SCALE"
    return _events(f"VERDICT: {verdict}\nSCORE: 70\nCONFIDENCE: MEDIUM\nREASONS:\n1. a\n2. b\n3. c\n")


def test_harness_worker_scores_fresh_and_reports(uploads, cli):
    h = _harness()
    c = collection.create(SRC, "Harness", children=2)
    slug = pipeline.llm_slug({"provider": "claude_code", "model": "claude-opus-5-5"})
    for kid in c["child_ids"]:                                    # what --vision shared copies in
        for v in VIDEOS:
            d = uploads / kid / "runs" / v / "llm" / slug
            d.mkdir(parents=True)
            (d / "features.json").write_text(json.dumps({"hook_strength": "strong", "frames": []}))
            (d / "score_p6.json").write_text(json.dumps({"verdict": "SCALE", "raw_output": "stale"}))
    fake = cli(_scorer)
    results = {}
    for i, kid in enumerate(c["child_ids"], 1):
        r = h._run_phase(kid, "claude_code", "claude-opus-5-5", None, "shared")
        assert r["status"] == "done", r
        results[kid] = dict(r, index=i, elapsed_s=5.0)
    assert len(fake.calls) == 2 * 2 * len(VIDEOS)                 # P6 + P5 per video per run: the stale cache was dropped
    r1 = results[c["child_ids"][0]]
    assert r1["videos"]["v1"]["final"] == "SCALE" and r1["videos"]["v2"]["final"] == "DO_NOT_SCALE"
    assert r1["videos"]["v2"]["p5_final"] == "SCALE"
    assert r1["comparison"]["basis"] == "cac_outcome" and r1["comparison"]["p6"]["accuracy"] == 1.0
    assert r1["comparison"]["p5"]["fp"] == 2 and r1["gate"]["beats_baseline"] is True
    assert r1["preprocess_rerun"] == [] and r1["calls"][0]["cost_usd"] == 0.0123
    assert json.loads((uploads / c["child_ids"][0] / "session.json").read_text())["llm"]["provider"] == "claude_code"
    rep = h.build_report({"collection": c["id"], "name": "Harness", "source": SRC, "vision": "shared",
                          "llm": {"provider": "claude_code", "model": "claude-opus-5-5"},
                          "children": {**results, "x": {"status": "error", "index": 3, "error": "RateLimited: later"}}})
    assert rep["runs"] == {"planned": 3, "done": 2, "error": 1, "running_or_interrupted": 0}
    assert rep["accuracy"]["p6"]["mean"] == 1.0 and rep["accuracy"]["p5"]["mean"] == pytest.approx(0.333)
    assert rep["false_scale"]["p5"]["mean"] == 2 and rep["false_scale"]["p6_runs_with_any"] == 0
    assert rep["gate_pass_rate"] == 1.0 and rep["per_video"]["v2"]["p6"] == {"DO_NOT_SCALE": 2}
    assert rep["agreement_p5_p6"]["exact_verdict"]["mean"] == pytest.approx(0.333)
    assert rep["failures"] == [{"child": "x", "index": 3, "error": "RateLimited: later"}]
    assert "accuracy  P6 1.0" in h.render(rep)


def test_harness_refuses_missing_preprocess(uploads, cli):
    h = _harness()
    c = collection.create(SRC, "NoPre", children=1)
    (uploads / c["child_ids"][0] / "runs" / "v2" / "preprocess.json").unlink()
    with pytest.raises(RuntimeError, match="no cached preprocess for v2"):
        h._run_phase(c["child_ids"][0], "claude_code", "claude-opus-5-5", None, "shared")
