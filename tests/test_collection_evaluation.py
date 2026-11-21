"""Offline tests for evaluating every run of a collection (collection.evaluations, GET
/api/collections/<id>/evaluation). Cached scores only: no model, key or network."""

import pytest
from fastapi.testclient import TestClient

from app import collection, config, dataset, evaluate
from app.main import app
from tests.test_claude_code import SRC, uploads  # noqa: F401


def _acc(correct, n, recall=None, cac=None):
    return {"correct": correct, "n": n, "accuracy": round(correct / n, 3) if n else None, "recall": recall,
            "cac": cac or {"n": 0, "accuracy": None, "recall": None}}


def test_aggregate_skips_blank_runs_instead_of_counting_zero():
    runs = [{"verdict_accuracy": {"P6": _acc(8, 10, 1.0)}},
            {"verdict_accuracy": {"P6": _acc(6, 10, 0.5)}},
            {"verdict_accuracy": {"P6": _acc(0, 0)}}]          # not scored: n = 0, accuracy None
    m = collection._aggregate(runs)["P6"]["expected_verdict"]
    assert (m["runs"], m["mean_accuracy"], m["min_accuracy"], m["max_accuracy"], m["mean_recall"]) == (2, 0.7, 0.6, 0.8, 0.75)
    cac = collection._aggregate(runs)["P6"]["cac_outcome"]
    assert cac["runs"] == 0 and cac["mean_accuracy"] is None


def test_every_run_is_evaluated_and_the_open_session_stays_open(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    dataset._activate(SRC)
    r = collection.evaluations(cid)
    assert [x["index"] for x in r["runs"]] == [1, 2]
    assert r["evaluated"] == 2 and r["failed"] == 0
    assert all(x["error"] is None and x["videos"] and x["scored"] == 0 for x in r["runs"])
    assert config.DATASET_ID == SRC                              # restored


def test_a_failing_run_is_reported_and_does_not_stop_the_others(uploads, monkeypatch):
    cid = collection.create(SRC, "QA", 2)["id"]
    first = collection.get(cid)["child_ids"][0]
    real = evaluate.run

    def run(**kw):
        if config.DATASET_ID == first:
            raise RuntimeError("boom")
        return real(**kw)

    monkeypatch.setattr(evaluate, "run", run)
    r = collection.evaluations(cid)
    assert (r["evaluated"], r["failed"]) == (1, 1)
    assert r["runs"][0]["error"] == "RuntimeError: boom" and r["runs"][1]["error"] is None
    assert config.DATASET_ID is None                             # nothing was open before


def test_endpoint(uploads):
    cid = collection.create(SRC, "QA", 1)["id"]
    c = TestClient(app)
    assert c.get(f"/api/collections/{cid}/evaluation").json()["evaluated"] == 1
    assert c.get("/api/collections/col-nope/evaluation").status_code == 404


# ---- QA additions: scored runs, blank-not-zero end to end, 409, empty collection, restore on error

import csv
import json as _json

from app import jobs, pipeline


def _score(child, vid, verdict):
    s = dataset._load_session(config.UPLOADS / child)
    slug = pipeline.llm_slug(config.resolve_llm(s.get("llm")))
    d = config.UPLOADS / child / "runs" / vid / "llm" / slug
    d.mkdir(parents=True, exist_ok=True)
    for st in ("score_p5", "score_p6"):
        (d / f"{st}.json").write_text(_json.dumps({"verdict": verdict, "model_verdict": verdict}))


def test_scored_run_accuracy_and_unscored_run_is_blank_not_zero(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    c1, _c2 = collection.get(cid)["child_ids"]
    _score(c1, "v1", "SCALE")
    _score(c1, "v2", "DO_NOT_SCALE")
    _score(c1, "v3", "DO_NOT_SCALE")
    r = collection.evaluations(cid)
    by = {x["index"]: x for x in r["runs"]}
    assert by[1]["scored"] == 3 and by[2]["scored"] == 0
    p6 = "P6 multimodal, after rule guard"
    assert by[1]["verdict_accuracy"][p6]["accuracy"] == 1.0
    assert by[2]["verdict_accuracy"][p6]["accuracy"] is None      # blank, not 0
    agg = r["aggregate"][p6]["expected_verdict"]
    assert agg["runs"] == 1 and agg["mean_accuracy"] == 1.0, agg  # unscored run skipped, not 0


def test_empty_collection_and_409_while_running(uploads, monkeypatch):
    cid = collection.create(SRC, "QA", 0)["id"]
    c = TestClient(app)
    r = c.get(f"/api/collections/{cid}/evaluation")
    assert r.status_code == 200 and r.json()["runs"] == [] and r.json()["aggregate"] == {}
    monkeypatch.setattr(jobs, "running", lambda: True)
    assert c.get(f"/api/collections/{cid}/evaluation").status_code == 409


def test_open_session_restored_after_failure_and_ground_truth_untouched(uploads, monkeypatch):
    cid = collection.create(SRC, "QA", 2)["id"]
    kids = collection.get(cid)["child_ids"]
    gt = [(config.UPLOADS / k / "ground_truth.csv") for k in kids] + [config.UPLOADS / SRC / "ground_truth.csv"]
    before = [p.read_bytes() for p in gt if p.exists()]
    sess = [(config.UPLOADS / k / "session.json").read_bytes() for k in kids]
    dataset._activate(SRC)
    monkeypatch.setattr(evaluate, "run", lambda **kw: (_ for _ in ()).throw(ValueError("x")))
    r = collection.evaluations(cid)
    assert r["failed"] == 2 and r["aggregate"] == {}
    assert config.DATASET_ID == SRC
    assert [p.read_bytes() for p in gt if p.exists()] == before
    assert [(config.UPLOADS / k / "session.json").read_bytes() for k in kids] == sess


def test_child_without_ground_truth_is_blank(uploads):
    cid = collection.create(SRC, "QA", 1)["id"]
    k = collection.get(cid)["child_ids"][0]
    (config.UPLOADS / k / "ground_truth.csv").unlink(missing_ok=True)
    r = collection.evaluations(cid)
    assert r["runs"][0]["error"] is None
    assert all(a["accuracy"] is None for a in r["runs"][0]["verdict_accuracy"].values())


# ---- parent-level reviews

def _sheet(child, who, rows, submitted=True):
    s = dataset._load_session(config.UPLOADS / child)
    s.setdefault("manual_review", {})[who] = {"rows": rows, **({"submitted_at": "t"} if submitted else {})}
    dataset._save_session(s)


def _r(d, reason=""):
    return {"decision": d, "reason": reason, "minutes_spent": ""}


def test_collection_reviews_aggregate_merge_and_hide_drafts(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    v1, v2, v3 = sorted(r["video_id"] for r in csv.DictReader((config.UPLOADS / a / "videos.csv").open()))[:3]
    _sheet(a, "Ana", {v1: _r("SCALE", "x"), v2: _r("SCALE"), v3: _r("SCALE")})
    _sheet(b, "Bo", {v1: _r("scale", "X"), v2: _r("DO_NOT_SCALE")})
    _sheet(b, "Cy", {v1: _r("DO_NOT_SCALE")}, submitted=False)             # draft: hidden
    r = collection.reviews(cid)
    by = {v["video_id"]: v for v in r["videos"]}
    assert list(by[v1]["calls"]) == ["run 001 · Ana", "run 002 · Bo"]
    assert by[v1]["calls"]["run 002 · Bo"]["run"] == b and by[v1]["merged"]["count"] == 2
    assert by[v2]["merged"] is None and by[v2]["agreement"] == "split"
    assert by[v3]["merged"] is None and by[v3]["agreement"] is None          # one review: nothing to compare
    assert r["agreement"] == {"agree": 1, "n": 2}
    assert [x["error"] for x in r["runs"]] == [None, None] and config.DATASET_ID is None


def test_collection_reviews_without_reviews_and_endpoint(uploads):
    cid = collection.create(SRC, "QA", 1)["id"]
    c = TestClient(app)
    j = c.get(f"/api/collections/{cid}/reviews").json()
    assert j["videos"] and all(v["calls"] == {} and v["merged"] is None for v in j["videos"])
    assert c.get("/api/collections/col-nope/reviews").status_code == 404


def test_collection_reviews_409_while_running_and_empty(uploads, monkeypatch):
    from app import jobs
    cid = collection.create(SRC, "QA", 0)["id"]
    c = TestClient(app)
    r = c.get(f"/api/collections/{cid}/reviews")
    assert r.status_code == 200 and r.json()["runs"] == [] and r.json()["videos"] == []
    monkeypatch.setattr(jobs, "running", lambda: True)
    assert c.get(f"/api/collections/{cid}/reviews").status_code == 409


def test_collection_reviews_restores_open_session_and_writes_nothing(uploads, monkeypatch):
    cid = collection.create(SRC, "QA", 2)["id"]
    kids = collection.get(cid)["child_ids"]
    v1 = sorted(r["video_id"] for r in __import__("csv").DictReader((config.UPLOADS / kids[0] / "videos.csv").open()))[0]
    _sheet(kids[0], "Ana", {v1: _r("SCALE")})
    snap = {p: p.read_bytes() for p in config.UPLOADS.rglob("*") if p.is_file()}
    dataset._activate(SRC)
    real = dataset.reviews
    monkeypatch.setattr(dataset, "reviews", lambda: (_ for _ in ()).throw(ValueError("boom")))
    r = collection.reviews(cid)
    assert [x["error"] is not None for x in r["runs"]] == [True, True] and config.DATASET_ID == SRC
    monkeypatch.setattr(dataset, "reviews", real)
    collection.reviews(cid)
    assert config.DATASET_ID == SRC
    assert {p: p.read_bytes() for p in config.UPLOADS.rglob("*") if p.is_file()} == snap


def test_collection_reviews_none_reason_across_runs_merges(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    v1 = sorted(r["video_id"] for r in __import__("csv").DictReader((config.UPLOADS / a / "videos.csv").open()))[0]
    _sheet(a, "Ana", {v1: {"decision": "SCALE", "reason": None}})
    _sheet(b, "Ana", {v1: {"decision": "SCALE", "reason": ""}})
    v = {x["video_id"]: x for x in collection.reviews(cid)["videos"]}[v1]
    assert len(v["calls"]) == 2 and v["merged"]["count"] == 2        # same name in two runs: both kept


def test_collection_reviews_corrupt_child_session_does_not_abort(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    (config.UPLOADS / a / "session.json").write_text("{not json")
    r = collection.reviews(cid)
    assert len(r["runs"]) == 2 and r["runs"][0]["error"]             # corrupt run listed, other still read


def test_collection_reviews_runs_without_index_do_not_overwrite_each_other(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    v1 = sorted(r["video_id"] for r in __import__("csv").DictReader((config.UPLOADS / a / "videos.csv").open()))[0]
    for k in (a, b):
        s = dataset._load_session(config.UPLOADS / k)
        s["collection"].pop("index", None)
        dataset._save_session(s)
    _sheet(a, "Ana", {v1: _r("SCALE")})
    _sheet(b, "Ana", {v1: _r("DO_NOT_SCALE")})
    v = {x["video_id"]: x for x in collection.reviews(cid)["videos"]}[v1]
    assert len(v["calls"]) == 2


def test_collection_reviews_unique_collapses_copies_and_does_not_fake_agreement(uploads):
    cid = collection.create(SRC, "QA", 3)["id"]
    a, b, c = collection.get(cid)["child_ids"]
    v1, v2 = sorted(r["video_id"] for r in csv.DictReader((config.UPLOADS / a / "videos.csv").open()))[:2]
    for ch in (a, b, c):                                                  # Ana's sheet was copied into every run
        _sheet(ch, "Ana", {v1: _r("SCALE", "Strong"), v2: _r("SCALE")})
    _sheet(b, "Bo", {v1: _r("scale", " strong "), v2: _r("DO_NOT_SCALE")})
    _sheet(c, "Ana", {v1: _r("SCALE", "Strong"), v2: _r("DO_NOT_SCALE")})                            # in run 003 Ana changed her mind on v2
    full = {v["video_id"]: v for v in collection.reviews(cid)["videos"]}
    assert len(full[v1]["calls"]) == 4 and full[v1]["calls"]["run 001 · Ana"]["decision"] == "SCALE"
    u = collection.reviews(cid, unique=True)
    by = {v["video_id"]: v for v in u["videos"]}
    assert u["unique"] is True and list(by[v1]["calls"]) == ["Ana", "Bo"] and u["columns"] == ["Ana", "Bo"]
    ana = by[v1]["calls"]["Ana"]
    assert ana["count"] == 3 and len(ana["variants"]) == 1 and ana["variants"][0]["runs"] == ["001", "002", "003"]
    assert by[v1]["merged"]["count"] == 2 and by[v1]["agreement"] == "agree"   # two reviewers, one call
    va = by[v2]["calls"]["Ana"]["variants"]                                      # Ana changed her mind in run 003
    assert [(x["decision"], x["runs"]) for x in va] == [("SCALE", ["001", "002"]), ("DO_NOT_SCALE", ["003"])]
    assert by[v2]["agreement"] == "split" and by[v2]["merged"] is None
    # copies alone never count as agreement: one reviewer, three identical runs
    only = collection.create(SRC, "Solo", 2)["id"]
    for ch in collection.get(only)["child_ids"]:
        _sheet(ch, "Ana", {v1: _r("SCALE")})
    s = {v["video_id"]: v for v in collection.reviews(only, unique=True)["videos"]}[v1]
    assert s["agreement"] is None and s["merged"] is None and s["calls"]["Ana"]["count"] == 2
    assert {v["video_id"]: v for v in collection.reviews(only)["videos"]}[v1]["agreement"] == "agree"   # the full grid does
    assert TestClient(app).get(f"/api/collections/{cid}/reviews?unique=true").json()["unique"] is True


def _vids(child):
    import csv
    return sorted(r["video_id"] for r in csv.DictReader((config.UPLOADS / child / "videos.csv").open()))


def test_unique_param_parsing(uploads):
    cid = collection.create(SRC, "QA", 1)["id"]
    c = TestClient(app)
    for q, want in (("true", True), ("1", True), ("false", False), ("0", False)):
        j = c.get(f"/api/collections/{cid}/reviews?unique={q}")
        assert j.status_code == 200 and j.json()["unique"] is want, q
    assert c.get(f"/api/collections/{cid}/reviews").json()["unique"] is False
    assert c.get(f"/api/collections/{cid}/reviews?unique=banana").status_code == 422


def test_unique_empty_collection_and_error_run(uploads):
    cid = collection.create(SRC, "QA", 0)["id"]
    assert collection.reviews(cid, unique=True)["videos"] == []
    cid = collection.create(SRC, "QA", 2)["id"]
    a, _ = collection.get(cid)["child_ids"]
    (config.UPLOADS / a / "session.json").write_text("{bad")
    r = collection.reviews(cid, unique=True)
    assert r["runs"][0]["error"] and r["runs"][1]["error"] is None


def test_unique_none_vs_blank_reason_and_minutes_ignored_and_drafts_hidden(uploads):
    cid = collection.create(SRC, "QA", 3)["id"]
    a, b, c3 = collection.get(cid)["child_ids"]
    v = _vids(a)[0]
    _sheet(a, "Ana", {v: {"decision": "SCALE", "reason": None, "minutes_spent": "1"}})
    _sheet(b, "Ana", {v: {"decision": "scale", "reason": "  ", "minutes_spent": "9"}})
    _sheet(c3, "Ana", {v: {"decision": "DO_NOT_SCALE"}}, submitted=False)
    x = {y["video_id"]: y for y in collection.reviews(cid, unique=True)["videos"]}[v]
    assert list(x["calls"]) == ["Ana"] and x["calls"]["Ana"]["count"] == 2 and x["agreement"] is None


def test_unique_one_column_per_reviewer_variants_listed_in_run_order(uploads):
    cid = collection.create(SRC, "QA", 3)["id"]
    a, b, c3 = collection.get(cid)["child_ids"]
    v1, v2 = _vids(a)[:2]
    _sheet(a, "Ana", {v1: _r("SCALE"), v2: _r("SCALE")})
    _sheet(b, "Ana", {v1: _r("SCALE"), v2: _r("DO_NOT_SCALE")})
    _sheet(c3, "Ana", {v1: _r("DO_NOT_SCALE"), v2: _r("DO_NOT_SCALE")})
    r = collection.reviews(cid, unique=True)
    assert r["columns"] == ["Ana"]                                               # no synthesized variant columns
    by = {y["video_id"]: y for y in r["videos"]}
    runs = lambda v: [(x["decision"], x["runs"]) for x in by[v]["calls"]["Ana"]["variants"]]
    assert runs(v1) == [("SCALE", ["001", "002"]), ("DO_NOT_SCALE", ["003"])]
    assert runs(v2) == [("SCALE", ["001"]), ("DO_NOT_SCALE", ["002", "003"])]


def test_unique_reviewer_named_like_a_variant_does_not_collide(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    v = _vids(a)[0]
    _sheet(a, "Ana", {v: _r("SCALE")})
    _sheet(b, "Ana", {v: _r("DO_NOT_SCALE")})
    _sheet(a, "Ana (variant 2)", {v: _r("SCALE", "other person")})
    x = {y["video_id"]: y for y in collection.reviews(cid, unique=True)["videos"]}[v]
    assert sum(c["count"] for c in x["calls"].values()) == 3


def test_unique_single_reviewer_is_not_agreement(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    v = _vids(a)[0]
    _sheet(a, "Ana", {v: _r("SCALE", "x")})
    _sheet(b, "Ana", {v: _r("SCALE", "y")})
    r = collection.reviews(cid, unique=True)
    assert {y["video_id"]: y for y in r["videos"]}[v]["agreement"] is None


def test_unique_run_list_order_beyond_999(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    a, b = collection.get(cid)["child_ids"]
    for k, i in ((a, 999), (b, 1000)):
        s = dataset._load_session(config.UPLOADS / k); s["collection"]["index"] = i; dataset._save_session(s)
    v = _vids(a)[0]
    _sheet(a, "Ana", {v: _r("SCALE")}); _sheet(b, "Ana", {v: _r("SCALE")})
    x = {y["video_id"]: y for y in collection.reviews(cid, unique=True)["videos"]}[v]
    assert x["calls"]["Ana"]["variants"][0]["runs"] == ["999", "1000"]
