"""Offline checks for the sessions-first screen layout: the landing screen lists parent sessions, and
Review / Evaluation / Recap are sub-items of an open session. The UI is static JS, so these check the
page structure and the API data it is built from; no model, key or network."""

import re
import shutil
import subprocess

import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from app import collection
from app.main import app
from tests.test_claude_code import SRC, uploads  # noqa: F401

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _static(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_only_sessions_is_a_top_level_tab_the_rest_sit_inside_a_session():
    html = _static("index.html")
    top, inner = html.split('id="session-tabs"', 1)
    assert 'id="tab-home"' in top
    assert not any(f'id="tab-{t}"' in top for t in ("setup", "review", "eval", "recap"))
    inner = inner.split("</nav>", 1)[0]
    assert all(f'id="tab-{t}"' in inner for t in ("setup", "review", "eval", "recap"))
    assert re.search(r'id="session-tabs"[^>]*\bhidden\b', html)        # hidden until a session is open


def test_app_js_is_valid_and_wires_the_parent_dashboard():
    js = _static("app.js")
    assert 'view: "home"' in js and '"evaluation"' in js and "data-open-collection" in js
    if shutil.which("node"):
        p = subprocess.run(["node", "--check", str(STATIC / "app.js")],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


def test_parent_and_child_data_the_screens_are_built_from(uploads):
    cid = collection.create(SRC, "QA", 2)["id"]
    c = TestClient(app)
    sessions = c.get("/api/sessions").json()
    kids = [s for s in sessions if (s.get("collection") or {}).get("id") == cid]
    assert len(kids) == 2 and not (next(s for s in sessions if s["id"] == SRC).get("collection"))
    dash = c.get(f"/api/collections/{cid}/evaluation").json()
    assert {r["id"] for r in dash["runs"]} == {k["id"] for k in kids}   # each dashboard row opens a child
    assert c.get("/api/collections").json()[0]["children"] == 2


def test_results_screen_shows_one_measure_with_its_basis():
    js = _static("app.js")
    assert "const evalBasis" in js and "evalBasisName" in js
    assert "Measured against the expected verdict" not in js and "vs expected verdict</th><th colspan=\"3\">" not in js
    assert 'rowspan="2"><b>${esc(m.reviewer)}' not in js          # one row per reviewer now


def test_open_session_lands_on_results_and_breadcrumb_follows_renames():
    js = _static("app.js")
    assert 'data-scored="${+x.scored || 0}"' in js                      # the button carries the scored count
    assert 'then === "eval" || scored > 0 ? "eval" : "setup"' in js     # scored -> Evaluation, else Dataset
    assert "+t.dataset.scored || 0" in js
    assert js.count("refreshCrumb()") >= 5 and "function refreshCrumb" in js
    assert "app.js?v=39" in _static("index.html")
    if shutil.which("node"):
        p = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


def test_form_schema_loaded_before_setform_from_landing():
    """The landing screen never loads the form schema, so every path that calls setForm / clearForm
    from there must `await loadForm()` first (regression: "Cannot read properties of null (reading 'files')")."""
    js = _static("app.js")
    open_fn = js[js.index("async function openSession"):js.index("// Landing screen")]
    assert open_fn.index("await loadForm()") < open_fn.index("setForm(s.form)")
    reset_fn = js[js.index("async function resetDataset"):js.index("async function renderSetup")]
    assert reset_fn.index("await loadForm()") < reset_fn.index("clearForm()")
    assert "state.session = null; await loadForm(); clearForm();" in js


def test_reviews_merge_identical_calls_into_one_entry(uploads):
    from app import config, dataset
    dataset._activate(SRC)
    s = dataset._load_session(config.UPLOADS / SRC)
    vids = list(dataset.ingest.load_inputs()[0])[:3]

    def row(d, r="", m=""):
        return {"decision": d, "reason": r, "minutes_spent": m}
    s["manual_review"] = {
        "Ana": {"submitted_at": "t", "rows": {vids[0]: row("SCALE", "Strong saves", "2"), vids[1]: row("SCALE", "good"),
                                             vids[2]: row("SCALE")}},
        "Bo": {"submitted_at": "t", "rows": {vids[0]: row("scale", " strong  SAVES ", "5"), vids[1]: row("SCALE", "fine"),
                                            vids[2]: row("DO_NOT_SCALE")}},
        "Cy": {"rows": {vids[0]: row("DO_NOT_SCALE")}},                       # draft: hidden, not compared
    }
    dataset._save_session(s)
    by = {v["video_id"]: v for v in dataset.reviews()["videos"]}
    m = by[vids[0]]["merged"]
    assert m["reviewers"] == ["Ana", "Bo"] and m["count"] == 2 and m["decision"] == "SCALE"
    assert by[vids[1]]["merged"] is None and by[vids[1]]["agreement"] == "agree"   # same verdict, different reason
    assert by[vids[2]]["merged"] is None and by[vids[2]]["agreement"] == "split"
    assert by[vids[0]]["calls"]["Ana"]["decision"] == "SCALE"                      # per-reviewer detail still there
    assert "v.merged" in _static("app.js")


def _reviews_with(dataset, config, sheets):
    s = dataset._load_session(config.UPLOADS / SRC)
    s["manual_review"] = sheets
    dataset._save_session(s)
    return {x["video_id"]: x for x in dataset.reviews()["videos"]}


def test_reviews_merge_edge_cases(uploads):
    from app import config, dataset
    dataset._activate(SRC)
    v = list(dataset.ingest.load_inputs()[0])

    def row(d, r="", m=""):
        return {"decision": d, "reason": r, "minutes_spent": m}
    by = _reviews_with(dataset, config, {
        "A": {"submitted_at": "t", "rows": {v[0]: row("SCALE", "x"), v[1]: row("SCALE", "x"), v[2]: row("SCALE", "x")}},
        "B": {"submitted_at": "t", "rows": {v[0]: row("SCALE", "x"), v[1]: row("SCALE", "x"), v[2]: row("DO_NOT_SCALE", "x")}},
        "C": {"submitted_at": "t", "rows": {v[0]: row("SCALE", "x"), v[1]: row("", "")}},
    })
    assert by[v[0]]["merged"]["count"] == 3                       # 3 of 3
    assert by[v[1]]["merged"]["reviewers"] == ["A", "B"]          # 2 of 3 (C left it blank)
    assert by[v[2]]["merged"] is None and by[v[2]]["agreement"] == "split"
    assert by[v[1]]["calls"].keys() == {"A", "B"}


def test_reviews_merge_none_reason_equals_blank_reason(uploads):
    from app import config, dataset
    dataset._activate(SRC)
    v = list(dataset.ingest.load_inputs()[0])[0]
    by = _reviews_with(dataset, config, {
        "A": {"submitted_at": "t", "rows": {v: {"decision": "SCALE", "reason": None}}},
        "B": {"submitted_at": "t", "rows": {v: {"decision": "SCALE", "reason": ""}}}})
    assert by[v]["merged"] is not None                            # missing reason must not read as the word "None"


def test_reviews_merge_zero_and_single_reviewer(uploads):
    from app import config, dataset
    dataset._activate(SRC)
    assert all(x["merged"] is None for x in dataset.reviews()["videos"])
    s = dataset._load_session(config.UPLOADS / SRC)
    vid = list(dataset.ingest.load_inputs()[0])[0]
    s["manual_review"] = {"A": {"submitted_at": "t", "rows": {vid: {"decision": "SCALE", "reason": "r"}}}}
    dataset._save_session(s)
    assert all(x["merged"] is None for x in dataset.reviews()["videos"])


def test_collection_dashboard_shows_only_the_cac_outcome_measure():
    js = _static("app.js")
    dash = js[js.index("function collectionDashboard"):js.index("async function renderResults")]
    assert "vs CAC outcome" in dash and "vs expected verdict" not in dash
    assert "expected_verdict" not in dash and "verdict_accuracy[n]?.cac" in dash


def test_collection_dashboard_renders_blank_and_error_runs_without_throwing(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = tmp_path / "t.js"
    js.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const body = src.slice(src.indexOf('function collectionDashboard'), src.indexOf('// Every run'));
const esc = (s) => String(s ?? '');
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
eval(body + ';globalThis.f = collectionDashboard');
const agg = {P6: {cac_outcome: {runs: 0, mean_accuracy: null, min_accuracy: null, max_accuracy: null, mean_recall: null}}};
const h = f({name: 'c', evaluated: 1, failed: 1, aggregate: agg, runs: [
  {id: 'a', name: 'a', llm: {}, scored: 0, videos: 3, error: null, verdict_accuracy: {P6: {}}},
  {id: 'b', name: null, llm: {}, error: 'X: y', verdict_accuracy: null, scored: null, videos: null},
  {id: 'c', llm: {provider: 'p'}, scored: 1, videos: 3, error: null, verdict_accuracy: {P6: {cac: {n: 2, correct: 1, accuracy: 0.5}}}}]});
if (/NaN|undefined|null|\b0%/.test(h) || !h.includes('50%')) { console.log(h); process.exit(1); }
""")
    p = subprocess.run(["node", str(js), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_collection_has_its_own_hash_routes_and_a_menu(tmp_path):
    js = _static("app.js")
    assert "hashchange" in js and "data-coll-tab" in js and "routeFromHash();" in js
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('const COLL_TABS'), b = src.indexOf('// Dashboard: the evaluation');
const c = src.indexOf('function collectionMenu'), d = src.indexOf('function routeFromHash');
const esc = (s) => String(s ?? '');
eval(src.slice(a, b) + src.slice(c, d) + ';globalThis.p = parseCollRoute; globalThis.r = collRoute; globalThis.m = collectionMenu');
const ok = (x) => { if (!x) { console.log('FAIL'); process.exit(1); } };
ok(JSON.stringify(p('#/collections/col-1')) === '{"cid":"col-1","tab":"dashboard"}');
ok(JSON.stringify(p('#/collections/col-1/reviews')) === '{"cid":"col-1","tab":"reviews"}');
ok(p('') === null && p('#/other') === null && p('#/collections/') === null);
ok(p(r('col-1', 'reviews')).tab === 'reviews' && p(r('col-1', 'dashboard')).tab === 'dashboard');
const h = m('col-1', 'reviews');
ok(h.includes('>Dashboard<') && h.includes('>Reviews<') && /active" role="tab" data-coll-tab="reviews"/.test(h));
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def _parse_routes(tmp_path, hashes):
    js = tmp_path / "r.js"
    js.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
eval(src.slice(src.indexOf('const collRoute'), src.indexOf('// Dashboard: the evaluation')) + ';globalThis.P = parseCollRoute; globalThis.R = collRoute');
const out = [];
for (const h of JSON.parse(process.argv[3])) { try { out.push(P(h)); } catch (e) { out.push('THROW ' + e.message); } }
out.push(P(R('a b/<x>&"', 'reviews')));
console.log(JSON.stringify(out));
""")
    p = subprocess.run(["node", str(js), str(STATIC / "app.js"), __import__("json").dumps(hashes)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return __import__("json").loads(p.stdout)


def test_parse_coll_route_edge_cases(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    d, rv = {"cid": "x", "tab": "dashboard"}, {"cid": "x", "tab": "reviews"}
    got = _parse_routes(tmp_path, ["", "#", "#/", "#/collections/", "#/collections/x", "#/collections/x/",
                                   "#/collections/x/reviews/", "#/collections/x/foo", "#/collections/x/reviews/y"])
    assert got[:9] == [None, None, None, None, d, d, rv, None, None]
    assert got[9] == {"cid": 'a b/<x>&"', "tab": "reviews"}          # encode/decode round-trips


def test_parse_coll_route_malformed_percent_escape_does_not_throw(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    got = _parse_routes(tmp_path, ["#/collections/%zz"])
    assert got[0] is None


def test_collection_reviews_unique_toggle_renders(tmp_path):
    js = _static("app.js")
    assert 'id="coll-unique"' in js and "reviews?unique=" in js
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const body = src.slice(src.indexOf('function collectionReviews'), src.indexOf('async function renderResults'));
const esc = (s) => String(s ?? ''); const CALL = {SCALE: 'Scale'}; const label = (v) => v;
eval(body + ';globalThis.f = collectionReviews');
const call = {reviewer: 'Ana', count: 3, variants: [{decision: 'SCALE', reason: '', runs: ['001', '002', '003'], count: 3}, {decision: 'DO_NOT_SCALE', reason: 'x', runs: ['004'], count: 1}]};
const h = f({name: 'c', unique: true, columns: ['Ana'], runs: [], agreement: {n: 0, agree: 0}, videos: [
  {video_id: 'v', creator: null, caption: null, merged: null, agreement: null, calls: {Ana: call}}]});
if (!/id="coll-unique" checked/.test(h) || !h.includes('3× · runs 001, 002, 003') || !h.includes('run 004') || /undefined|NaN/.test(h)) { console.log(h); process.exit(1); }
const g = f({name: 'c', unique: false, columns: ['run 001 · Ana'], runs: [], agreement: {n: 0, agree: 0}, videos: [
  {video_id: 'v', merged: null, agreement: null, calls: {'run 001 · Ana': {decision: 'SCALE', reason: ''}}}]});
if (/coll-unique" checked/.test(g) || g.includes('×')) { console.log(g); process.exit(1); }
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_run_prompt_selector_is_wired_and_cli_has_the_flag():
    js = _static("app.js")
    assert 'id="run-prompt"' in js and "prompt_id: state.runPrompt || null" in js and "?prompt=" in js
    cli = (Path(__file__).resolve().parent.parent / "scripts" / "score_video.py").read_text(encoding="utf-8")
    assert '"--prompt"' in cli and "prompt_id=prompt_id" in cli


def test_chosen_run_prompt_is_reset_when_a_session_is_opened():
    js = _static("app.js")
    open_fn = js[js.index("async function openSession"):js.index("// Landing screen")]
    assert 'state.runPrompt = ""' in open_fn


def test_prompts_module_has_its_own_tab_route_and_pages_render(tmp_path):
    html, js = _static("index.html"), _static("app.js")
    top = html.split('id="session-tabs"', 1)[0]
    assert 'id="tab-prompts"' in top and '"#/prompts"' in js and "openPromptLibrary" in js
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('const pvMetric'), b = src.indexOf('function pvPoll');
const pv = src.slice(a, b);                                    // pvResult, renderPrompts, library page, pvJobLine
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const localTime = (x) => x; const int = (x) => String(x);
const state = { view: 'prompts', pvEdit: null, pvView: null, pvOverride: null };
const main = { innerHTML: '' }, evalBody = { innerHTML: '' };
const $ = (s) => (s === '#eval-body' ? evalBody : null);
const setView = () => {}; const pvPoll = () => {};
const list = {active: 'P6', baseline: 'P5', adoptions: [], versions: [
  {id: 'P5', parent: null, note: 'base', builtin: true, sha256: 'a'.repeat(64), chars: 10, created_at: null, active: false},
  {id: 'P6', parent: 'P5', note: 'built', builtin: true, sha256: 'b'.repeat(64), chars: 11, created_at: null, active: true},
  {id: 'P6.2', parent: 'P6', note: '<img src=x onerror=alert(1)>', builtin: false, sha256: 'c'.repeat(64), chars: 12, created_at: 't', active: false}]};
const api = async (p) => (p === '/api/prompts' ? list : p === '/api/iterations' ? [] : { running: false });
eval(pv + ';globalThis.lib = renderPromptLibrary; globalThis.sess = renderPrompts');
const ok = (x, m) => { if (!x) { console.log('FAIL', m); process.exit(1); } };
(async () => {
  await lib();
  const h = main.innerHTML;
  ok(h.includes('data-pv="new" data-id="P6.2"') && h.includes('data-pv="delete" data-id="P6.2"'), 'library actions');
  ok(!h.includes('data-pv="new" data-id="P5"') && !h.includes('data-pv="delete" data-id="P6"'), 'P5/P6 read only');
  ok(!h.includes('data-pv="run"') && !h.includes('data-pv="adopt"'), 'no iteration or adopt in the library');
  ok(!h.includes('<img') && h.includes('&lt;img'), 'escaped');
  state.pvEdit = { parent: 'P6', text: '{video_summary} <b>', note: '' };
  await lib();
  ok(main.innerHTML.includes('&lt;b&gt;') && main.innerHTML.includes('data-pv="save"'), 'editor');
  state.view = 'eval'; state.pvEdit = null;
  await sess();
  const s = evalBody.innerHTML;
  ok(s.includes('data-pv="run" data-id="P6.2"') && s.includes('data-pv="adopt" data-id="P6.2"') && s.includes('data-pv="library"'), 'session tab');
  ok(!s.includes('data-pv="new"') && !s.includes('data-pv="delete"') && !s.includes('pv-text'), 'no editing in the session tab');
  ok(!s.includes('<img'), 'escaped');
})();
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_run_prompt_choice_follows_the_selector_and_falls_back_when_missing():
    js = _static("app.js")
    assert 'ev.target.id === "run-prompt") { state.runPrompt = ev.target.value;' in js
    assert "!(state.promptList?.versions || []).some((v) => v.id === state.runPrompt)) state.runPrompt = \"\"" in js


def test_static_and_index_are_revalidated_so_edited_js_is_never_stale():
    # A stale cached app.js (same ?v=) hid the Prompts page; the browser must revalidate.
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.get("/").headers.get("cache-control") == "no-cache"
    r = c.get("/static/app.js")
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache"


def test_two_way_run_on_choice_shows_availability_without_a_model_call_or_a_key(uploads, monkeypatch):
    from app import config, dataset, llm
    dataset._activate(SRC)
    real = config._env
    monkeypatch.setattr(config, "_env", lambda k, *a: "sk-ant-SECRETVALUE" if k == "ANTHROPIC_API_KEY" else real(k, *a))
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", "/nonexistent/claude")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("no model call"))
    c = TestClient(app)
    r = c.get("/api/llm")
    info = {p["id"]: p for p in r.json()["providers"]}
    assert info["anthropic"]["configured"] is True and info["claude_code"]["configured"] is False
    assert "SECRETVALUE" not in r.text                                        # the env name may show, never the key
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", __file__)                  # any existing file counts as "found"
    j = c.put("/api/llm", json={"provider": "claude_code", "model": ""}).json()   # what the radio sends
    assert j["current"] == {"provider": "claude_code", "model": config.DEFAULT_MODELS["claude_code"]}
    assert dataset._load_session(config.UPLOADS / SRC)["llm"]["provider"] == "claude_code"     # per session, persisted
    assert c.put("/api/llm", json={"provider": "anthropic", "model": ""}).json()["current"]["provider"] == "anthropic"


def test_run_on_choice_renders_disabled_with_reason(tmp_path):
    js = _static("app.js")
    assert 'name="llm-which"' in js and "saveLlm(ev.target.value)" in js and "app.js?v=39" in _static("index.html")
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const body = src.slice(src.indexOf('const LLM_TWO'), src.indexOf('async function renderLlmPicker'));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
eval(body + ';globalThis.f = llmTwoChoice');
const ok = (x, m) => { if (!x) { console.log('FAIL', m); process.exit(1); } };
const P = (id, configured, extra = {}) => ({id, configured, env: id === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'CLAUDE_CODE_BIN', missing_sdk: null, ...extra});
let h = f({current: {provider: 'claude_code'}, providers: [P('claude_code', true), P('anthropic', false)]}, false);
ok(/value="claude_code"\s+checked\s+>/.test(h) && /value="anthropic"\s+disabled/.test(h) && h.includes('ANTHROPIC_API_KEY is not set'), 'anthropic disabled with reason');
h = f({current: {provider: 'anthropic'}, providers: [P('claude_code', false), P('anthropic', true)]}, false);
ok(/value="claude_code"\s+disabled/.test(h) && h.includes('claude CLI was not found') && /value="anthropic"\s+checked/.test(h), 'cli disabled with reason');
h = f({current: {provider: 'ollama'}, providers: [P('claude_code', true), P('anthropic', true)]}, true);
ok((h.match(/disabled/g) || []).length === 2 && h.includes('Using ollama'), 'disabled while a job runs; other provider noted');
ok(!/undefined|null/.test(h), 'clean');
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_run_on_choice_never_leaks_a_key_spawns_nothing_and_is_refused_while_running(uploads, monkeypatch):
    import subprocess as sp
    from app import config, dataset, jobs, llm
    dataset._activate(SRC)
    real = config._env
    monkeypatch.setattr(config, "_env", lambda k, *a: "sk-ant-DISTINCTIVE123" if k == "ANTHROPIC_API_KEY" else real(k, *a))
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", __file__)
    for name in ("run", "Popen", "check_output"):
        monkeypatch.setattr(sp, name, lambda *a, **k: pytest.fail("no process may be spawned"))
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("no model call"))
    c = TestClient(app)
    seen = [c.get("/api/llm").text, c.put("/api/llm", json={"provider": "anthropic", "model": ""}).text,
            c.put("/api/llm", json={"provider": "nope", "model": ""}).text,
            c.put("/api/llm", json={"provider": "anthropic", "model": "bad model!"}).text,
            c.get("/api/llm/models?provider=claude_code").text,
            (config.UPLOADS / SRC / "session.json").read_text()]
    assert not any("DISTINCTIVE123" in s for s in seen)
    monkeypatch.setattr(jobs, "running", lambda: True)
    r = c.put("/api/llm", json={"provider": "claude_code", "model": ""})
    assert r.status_code == 409
    assert dataset._load_session(config.UPLOADS / SRC)["llm"]["provider"] == "anthropic"      # unchanged by the refused PUT


def test_switching_provider_keeps_separate_cache_folders_and_resets_the_model_to_the_default(uploads):
    from app import config, dataset, pipeline
    dataset._activate(SRC)
    c = TestClient(app)
    c.put("/api/llm", json={"provider": "anthropic", "model": "claude-custom-1"})
    a = pipeline.llm_slug(config.llm_choice())
    j = c.put("/api/llm", json={"provider": "claude_code", "model": ""}).json()
    b = pipeline.llm_slug(config.llm_choice())
    assert a == "anthropic--claude-custom-1" and b.startswith("claude_code--") and a != b
    assert j["current"]["model"] == config.DEFAULT_MODELS["claude_code"]
    back = c.put("/api/llm", json={"provider": "anthropic", "model": ""}).json()["current"]["model"]
    assert back == config.DEFAULT_MODELS["anthropic"]            # radio switch uses the provider default; a typed model is not remembered


def test_run_on_choice_escapes_provider_and_reason_text(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const body = src.slice(src.indexOf('const LLM_TWO'), src.indexOf('async function renderLlmPicker'));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
eval(body + ';globalThis.f = llmTwoChoice');
const evil = '<img src=x onerror=alert(1)>';
const h = f({current: {provider: evil}, providers: [
  {id: 'claude_code', configured: false, env: evil, missing_sdk: evil},
  {id: 'anthropic', configured: false, env: '"><script>x</script>', missing_sdk: null}]}, false);
if (/<img|<script/.test(h)) { console.log(h); process.exit(1); }
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_failed_provider_switch_rerenders_the_radio_to_the_real_state():
    js = _static("app.js")
    fn = js[js.index("async function saveLlm"):js.index("// Prompt versions and iterations")]
    catch = fn[fn.index("catch (e)"):fn.index("return;")]     # the failure branch
    assert "renderLlmPicker" in catch


def test_put_llm_refuses_an_unavailable_provider_with_the_reason_but_never_the_key(uploads, monkeypatch):
    from app import config, dataset
    dataset._activate(SRC)
    real = config._env
    monkeypatch.setattr(config, "_env", lambda k, *a: None if k == "ANTHROPIC_API_KEY" else real(k, *a))
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", "/nonexistent/claude")
    c = TestClient(app)
    r = c.put("/api/llm", json={"provider": "anthropic", "model": ""})
    assert r.status_code == 400 and "ANTHROPIC_API_KEY is not set" in r.json()["detail"]
    r = c.put("/api/llm", json={"provider": "claude_code", "model": ""})
    assert r.status_code == 400 and "claude CLI was not found" in r.json()["detail"]
    assert "llm" not in dataset._load_session(config.UPLOADS / SRC)             # nothing was saved
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", __file__)
    assert c.put("/api/llm", json={"provider": "claude_code", "model": ""}).status_code == 200


@pytest.mark.parametrize("raw, shown", [
    ("http://user:pa55@host.example:11434/v1?token=SECRET#frag", "http://host.example:11434/v1"),
    ("http://localhost:1234/v1", "http://localhost:1234/v1"),
    ("http://[::1]:8080", "http://[::1]:8080"),
    ("not a url", "(invalid URL)"),
])
def test_base_urls_are_redacted_in_api_llm(uploads, monkeypatch, raw, shown):
    from app import config, dataset
    dataset._activate(SRC)
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", raw)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", raw)
    r = TestClient(app).get("/api/llm")
    urls = [p["base_url"] for p in r.json()["providers"] if p["id"] in ("ollama", "openai_compatible")]
    assert urls == [shown, shown]
    assert "pa55" not in r.text and "SECRET" not in r.text


def test_views_follow_the_runs_prompt_and_the_selector_only_picks_the_next_run():
    js = _static("app.js")
    assert 'const videosUrl = () => "/api/videos"' in js                       # no ?prompt=: the server defaults to the run's
    assert "?prompt=${encodeURIComponent(state.runPrompt)}" not in js and "&prompt=${encodeURIComponent(state.runPrompt)}" not in js
    assert 'state.runPrompt = ev.target.value; refreshRunLine()' in js and "Prompt for the next run" in js
    assert "app.js?v=39" in _static("index.html")


def test_evaluation_relabels_p6_rows_with_the_evaluated_version(tmp_path):
    js = _static("app.js")
    assert "Methods evaluated" in js and "app.js?v=39" in _static("index.html")
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('let evalP'), b = src.indexOf('// A result row or column named after a prompt');
const c = src.indexOf('const shown ='), d = src.indexOf('const STAGE_NAMES');
const esc = (s) => String(s ?? ''); const evalBasis = () => 'cac_outcome'; const evalBasisName = {cac_outcome: 'real CAC outcome'};
eval(src.slice(a, b) + src.slice(c, d) + ';globalThis.setP = (p) => { evalP = p; }; globalThis.pname = pname; globalThis.panel = classificationPanel;');
const ok = (x, m) => { if (!x) { console.log('FAIL', m); process.exit(1); } };
const conf = {n: 2, tp: 1, fp: 0, fn: 1, tn: 0, recall: 0.5, precision: 1, f1: 0.67, accuracy: 0.5, winners: 2};
const m = () => ({all: {'Human (blind)': conf, 'P5 text-only': conf, 'P6 multimodal': conf}, same: {'Human (blind)': conf, 'P5 text-only': conf, 'P6 multimodal': conf}, videos: 2, winners: 2, never_scale_accuracy: 0});
const cls = {expected_verdict: m(), cac_outcome: m()};
setP('P6.2');
let h = panel(cls, {p5: true, p6: true});
ok(h.includes('P6.2 multimodal') && !/>P6 multimodal</.test(h) && h.includes('P5 and P6.2'), 'relabelled');
ok(pname('P6 multimodal, after rule guard') === 'P6.2 multimodal, after rule guard' && pname('P6.3 x') === 'P6.3 x', 'pname');
setP('P6');
ok(panel(cls, {p5: true, p6: true}).includes('>P6 multimodal<'), 'default label unchanged');
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_evaluation_pname_edge_cases_and_escaping(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
eval(src.slice(src.indexOf('let evalP'), src.indexOf('// A result row or column named after a prompt')) + ';globalThis.setP = (p) => { evalP = p; }; globalThis.pname = pname;');
setP('P6.2');
const cases = {'P6 multimodal': 'P6.2 multimodal', 'P6.20 x': 'P6.20 x', 'P6.2 x': 'P6.2 x', 'xP6 y': 'xP6 y', 'P6x': 'P6x', 'P6_1': 'P6_1',
               'P5 text-only': 'P5 text-only', 'AI (P6, P5)': 'AI (P6.2, P5)'};
for (const [i, o] of Object.entries(cases)) if (pname(i) !== o) { console.log('FAIL', i, '->', pname(i), 'want', o); process.exit(1); }
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout


def test_api_errors_carry_the_http_status():
    assert "err.status = r.status;" in _static("app.js")


def test_start_pipeline_confirms_before_any_call_and_dashboard_shows_the_run(tmp_path):
    js = _static("app.js")
    assert "window.confirm(text)" in js and "/api/pipeline/plan" in js and "await openSession(res.session_id" in js
    assert "<th>Prompt · model</th>" in js and "x.run.prompt_id" in js
    if not shutil.which("node"):
        pytest.skip("node not installed")
    t = tmp_path / "t.js"
    t.write_text(r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('function startChoice'), b = src.indexOf('async function saveLlm');
const esc = (s) => String(s ?? '');
eval(src.slice(a, b) + ';globalThis.sc = startChoice');
const ok = (x, m) => { if (!x) { console.log('FAIL', m); process.exit(1); } };
const asked = []; const yes = (t) => { asked.push(t); return true; }; const no = (t) => { asked.push(t); return false; };
ok(sc({action: 'first', message: 'M1'}, yes) === 'auto' && asked[0] === 'M1', 'first');
ok(sc({action: 'child', message: 'M2'}, yes) === 'auto', 'child confirms with auto');
ok(sc({action: 'child', message: 'M2'}, no) === null, 'cancel = nothing starts');
asked.length = 0;
ok(sc({action: 'ask', message_resume: 'R', message_child: 'C'}, no) === null && asked.join() === 'R,C', 'ask: both declined');
let n = 0; ok(sc({action: 'ask', message_resume: 'R', message_child: 'C'}, () => ++n === 2) === 'child', 'ask: second option');
ok(sc({action: 'ask', message_resume: 'R', message_child: 'C'}, () => true) === 'resume', 'ask: resume');
""")
    p = subprocess.run(["node", str(t), str(STATIC / "app.js")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout
