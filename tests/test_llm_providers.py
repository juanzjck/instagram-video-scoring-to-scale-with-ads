"""Offline tests for the non-Anthropic providers in app/llm.py (OpenAI, OpenAI-compatible, Gemini,
Ollama), the per-model cache folders and the session's model choice.

Every client is a fake: no key, no network, no local server needed.
"""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, dataset, llm, pipeline, scoring, vision
from app.main import app

P6_REPLY = """VERDICT: SCALE
SCORE: 74
SCORE_RANGE: 66-82
CONFIDENCE: MEDIUM
REASONS:
1. Save rate of 1.50% on 40,000 reach is above the campaign norm.
2. The product is on screen from 0:01."""

IMG = base64.standard_b64encode(b"\xff\xd8fake-jpeg").decode()
CONTENT = [{"type": "text", "text": "Frame kf00 at t=0s:"},
           {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": IMG}},
           {"type": "text", "text": "Return the content features."}]
SAMPLING = {"temperature", "top_p", "top_k", "seed", "frequency_penalty", "presence_penalty"}


@pytest.fixture(autouse=True)
def no_session(monkeypatch):
    monkeypatch.setattr(config, "DATA", None)


@pytest.fixture
def use(monkeypatch):
    """Point llm.complete() at a provider + model, as a session choice would."""
    def set_(provider, model="m-1"):
        monkeypatch.setattr(config, "llm_choice", lambda: {"provider": provider, "model": model})
    return set_


# ---------------------------------------------------------------- fakes

class FakeCompletions:
    def __init__(self, text="", finish="stop", refusal=None):
        self.text, self.finish, self.refusal, self.calls = text, finish, refusal, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = SimpleNamespace(content=self.text, refusal=self.refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish)],
                               usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7))


@pytest.fixture
def fake_openai(monkeypatch):
    def install(*args, **kw):
        comp = FakeCompletions(*args, **kw)
        comp.clients = []

        def make(base_url=None, api_key=None):
            comp.clients.append((base_url, api_key))
            return SimpleNamespace(chat=SimpleNamespace(completions=comp),
                                   models=SimpleNamespace(list=lambda: [SimpleNamespace(id="gpt-x"),
                                                                        SimpleNamespace(id="text-embedding-3")]))
        monkeypatch.setattr(llm, "openai_client", make)
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        return comp
    return install


class FakeGeminiModels:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.resp


def gemini_resp(text="hi", finish="STOP", block=None):
    return SimpleNamespace(
        prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name=block)) if block else None,
        candidates=[] if block else [SimpleNamespace(
            finish_reason=SimpleNamespace(name=finish),
            content=SimpleNamespace(parts=[SimpleNamespace(text="thinking...", thought=True),
                                           SimpleNamespace(text=text, thought=None)]))],
        usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=5, thoughts_token_count=3))


@pytest.fixture
def fake_gemini(monkeypatch):
    pytest.importorskip("google.genai")
    def install(resp):
        models = FakeGeminiModels(resp)
        monkeypatch.setattr(llm, "gemini_client", lambda: SimpleNamespace(models=models))
        return models
    return install


class FakeOllama:
    """Stands in for httpx.post / httpx.get against the Ollama API."""

    def __init__(self, caps=("completion",), reply="hi", done="stop", prompt_tokens=50):
        self.caps, self.reply, self.done, self.prompt_tokens, self.calls = list(caps), reply, done, prompt_tokens, []

    def _resp(self, url, data):
        return httpx.Response(200, json=data, request=httpx.Request("POST", url))

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if url.endswith("/api/show"):
            return self._resp(url, {"capabilities": self.caps})
        return self._resp(url, {"message": {"role": "assistant", "content": self.reply}, "done_reason": self.done,
                                "prompt_eval_count": self.prompt_tokens, "eval_count": 9})

    def get(self, url, timeout=None):
        return self._resp(url, {"models": [{"name": "qwen2.5vl:7b"}]})


@pytest.fixture
def fake_ollama(monkeypatch):
    def install(**kw):
        o = FakeOllama(**kw)
        monkeypatch.setattr(llm.httpx, "post", o.post)
        monkeypatch.setattr(llm.httpx, "get", o.get)
        return o
    return install


# ---------------------------------------------------------------- OpenAI

def test_openai_request_shape_images_schema_and_no_sampling(use, fake_openai):
    use("openai", "gpt-x")
    comp = fake_openai("{}")
    out = llm.complete(CONTENT, system="sys", max_tokens=99, json_schema=vision.SCHEMA)
    req = comp.calls[0]
    assert req["model"] == "gpt-x" and req["max_completion_tokens"] == 99
    assert req["messages"][0] == {"role": "system", "content": "sys"}
    parts = req["messages"][1]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{IMG}"}}
    fmt = req["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["schema"] is vision.SCHEMA and fmt["json_schema"]["strict"]
    assert not SAMPLING & set(req)
    assert (out["provider"], out["model"], out["input_tokens"], out["output_tokens"]) == ("openai", "gpt-x", 11, 7)


@pytest.mark.parametrize("kw", [{"refusal": "I can't help with that"}, {"finish": "content_filter"}])
def test_openai_refusal_raises(use, fake_openai, kw):
    use("openai")
    fake_openai("", **kw)
    with pytest.raises(RuntimeError, match="declined"):
        llm.complete("x")


def test_empty_truncated_answer_is_an_error_not_a_blank_result(use, fake_openai):
    use("openai")
    fake_openai("", finish="length")
    with pytest.raises(llm.LLMError, match="output budget"):
        llm.complete("x", max_tokens=50)


def test_openai_without_key_is_a_clear_error(use, monkeypatch):
    use("openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    with pytest.raises(llm.ProviderUnavailable, match="OPENAI_API_KEY"):
        llm.complete("x")


def test_openai_compatible_uses_the_local_base_url(use, fake_openai, monkeypatch):
    use("openai_compatible", "local-model")
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    comp = fake_openai("hello")
    assert llm.complete("x", max_tokens=10)["text"] == "hello"
    assert comp.clients[-1][0] == "http://localhost:1234/v1"
    assert comp.calls[0]["max_tokens"] == 10                    # local servers read max_tokens


def test_rule_guard_still_runs_after_a_non_anthropic_answer(use, fake_openai):
    use("openai", "gpt-x")
    fake_openai(P6_REPLY)
    row = {"hours_since_post": 20, "reach": 40000, "views": 1, "total_interactions": 1, "saved": 1, "shares": 1}
    out = scoring.score("P6", "=== VIDEO ===", row)
    assert out["verdict"] == "DEFER_TO_HUMAN" and out["model_verdict"] == "SCALE" and out["rule_override"]
    assert out["usage"]["provider"] == "openai" and out["usage"]["model"] == "gpt-x"


def test_missing_sdk_gives_the_pip_command(use, monkeypatch):
    use("openai")
    real = llm.importlib.util.find_spec
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None if name == "openai" else real(name))
    with pytest.raises(llm.ProviderUnavailable, match="pip install openai"):
        llm.complete("x")
    assert next(p for p in llm.providers() if p["id"] == "openai")["missing_sdk"] == "openai"


# ---------------------------------------------------------------- Gemini

def test_gemini_request_shape_and_answer(use, fake_gemini):
    use("gemini", "gemini-x")
    models = fake_gemini(gemini_resp("{}"))
    out = llm.complete(CONTENT, system="sys", max_tokens=99, json_schema=vision.SCHEMA)
    req = models.calls[0]
    cfg = req["config"]
    assert req["model"] == "gemini-x"
    assert cfg.system_instruction == "sys" and cfg.max_output_tokens == 99
    assert cfg.response_json_schema == vision.SCHEMA and cfg.response_mime_type == "application/json"
    assert cfg.temperature is None and cfg.top_p is None and cfg.top_k is None
    parts = req["contents"][0].parts
    assert parts[1].inline_data.mime_type == "image/jpeg" and parts[1].inline_data.data == b"\xff\xd8fake-jpeg"
    assert out["text"] == "{}"                                 # thought parts are left out
    assert (out["input_tokens"], out["output_tokens"]) == (20, 8)


@pytest.mark.parametrize("resp", [gemini_resp(finish="SAFETY"), gemini_resp(block="PROHIBITED_CONTENT")])
def test_gemini_blocked_answer_raises(use, fake_gemini, resp):
    use("gemini")
    fake_gemini(resp)
    with pytest.raises(RuntimeError, match="declined"):
        llm.complete("x")


# ---------------------------------------------------------------- Ollama

def test_ollama_vision_model_gets_images_in_order(use, fake_ollama):
    use("ollama", "qwen2.5vl:7b")
    o = fake_ollama(caps=("completion", "vision"), reply="{}")
    llm.complete(CONTENT, system="sys", max_tokens=99, json_schema=vision.SCHEMA)
    url, body = o.calls[-1]
    assert url.endswith("/api/chat") and body["model"] == "qwen2.5vl:7b" and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1]["images"] == [IMG] and "[image 1]" in body["messages"][1]["content"]
    assert body["format"] == vision.SCHEMA
    assert set(body["options"]) == {"num_predict", "num_ctx"}  # caps only, no sampling


def test_ollama_text_model_refuses_images_before_calling(use, fake_ollama):
    use("ollama", "llama3.2")
    o = fake_ollama(caps=("completion",))
    with pytest.raises(llm.VisionUnsupported, match="cannot read images"):
        llm.complete(CONTENT)
    assert not any(u.endswith("/api/chat") for u, _ in o.calls)


def test_ollama_text_only_call_works_on_any_model(use, fake_ollama):
    use("ollama", "llama3.2")
    fake_ollama(reply=P6_REPLY)
    out = llm.complete("score this")
    assert out["text"] == P6_REPLY and out["provider"] == "ollama"


def test_ollama_full_context_is_an_error_not_a_silent_cut(use, fake_ollama):
    use("ollama", "llama3.2")
    fake_ollama(prompt_tokens=config.OLLAMA_NUM_CTX)
    with pytest.raises(llm.LLMError, match="OLLAMA_NUM_CTX"):
        llm.complete("x")


def test_ollama_not_running(use, monkeypatch):
    use("ollama", "llama3.2")
    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(llm.httpx, "post", down)
    with pytest.raises(llm.ProviderUnavailable, match="not reachable"):
        llm.complete("x")


def test_ollama_model_list_flags_vision(fake_ollama):
    fake_ollama(caps=("completion", "vision"))
    r = llm.list_models("ollama")
    assert r["error"] is None and r["models"] == [{"id": "qwen2.5vl:7b", "vision": True}]


def test_openai_model_list_skips_non_chat_models(fake_openai):
    fake_openai()
    assert [m["id"] for m in llm.list_models("openai")["models"]] == ["gpt-x"]


# ---------------------------------------------------------------- strict JSON output

def test_parse_json_strips_fences_and_validates():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(llm.LLMError, match="malformed"):
        llm.parse_json("Sure! here it is: {")
    with pytest.raises(llm.LLMError, match="missing"):
        llm.parse_json('{"hook_strength": "strong"}', vision.SCHEMA)
    good = {"hook_strength": "medium", "hook_reason": "", "product_visible": False, "product_first_seen_s": None,
            "brand_match": "yes", "brand_match_reason": "", "cta_present": False, "cta_t_s": None,
            "face_on_screen": True, "safety_issues": [], "frames": [{"t_s": 0, "note": "n", "on_screen_text": None}]}
    assert llm.parse_json(json.dumps(good), vision.SCHEMA) == good
    with pytest.raises(llm.LLMError, match="hook_strength"):
        llm.parse_json(json.dumps(dict(good, hook_strength="great")), vision.SCHEMA)


# ---------------------------------------------------------------- per-model cache + session choice

def test_each_model_has_its_own_cache_folder(tmp_path):
    a, b = {"provider": "anthropic", "model": config.MODEL}, {"provider": "ollama", "model": "qwen2.5vl:7b"}
    assert pipeline.llm_slug(b) == "ollama--qwen2.5vl_7b"
    d = tmp_path / "v1" / "llm" / pipeline.llm_slug(b)
    d.mkdir(parents=True)
    (d / "score_p6.json").write_text("{}")
    assert pipeline.stage_file(tmp_path, "v1", "score_p6", b) == d / "score_p6.json"
    assert pipeline.stage_file(tmp_path, "v1", "score_p6", a) is None          # never another model's answer
    # a file from before models were selectable is read only by the model that wrote it
    (tmp_path / "v1" / "score_p6.json").write_text(json.dumps({"usage": {"model": config.MODEL}}))
    assert pipeline.stage_file(tmp_path, "v1", "score_p6", a) == tmp_path / "v1" / "score_p6.json"
    assert pipeline.stage_file(tmp_path, "v1", "score_p6", {"provider": "openai", "model": config.MODEL}) is None


@pytest.fixture
def open_session(tmp_path, monkeypatch):
    for k in ("UPLOADS", "DATASET_ID", "DATA", "MEDIA", "RUNS", "DECISIONS_LOG"):
        monkeypatch.setattr(config, k, getattr(config, k))
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    sid = "20260101-000000-000000"
    (tmp_path / sid).mkdir()
    (tmp_path / sid / "session.json").write_text(json.dumps({"id": sid, "name": "x", "form": {}}))
    config.activate(sid)
    return sid


def test_session_choice_is_saved_validated_and_reset(open_session):
    assert config.llm_choice() == config.default_llm()
    dataset.set_llm({"provider": "ollama", "model": "qwen2.5vl:7b"})
    assert config.llm_choice() == {"provider": "ollama", "model": "qwen2.5vl:7b"}
    with pytest.raises(ValueError, match="unknown provider"):
        dataset.set_llm({"provider": "skynet", "model": "x"})
    with pytest.raises(ValueError, match="not a valid model id"):
        dataset.set_llm({"provider": "ollama", "model": "../../etc"})
    dataset.set_llm({})
    assert config.llm_choice() == config.default_llm()


def test_llm_api_endpoints(open_session, fake_ollama, monkeypatch):
    fake_ollama(caps=("completion",))
    monkeypatch.setitem(config.DEFAULT_MODELS, "openai", None)
    c = TestClient(app)
    assert c.get("/api/llm").json()["current"] == config.default_llm()
    assert c.get("/api/llm/models", params={"provider": "nope"}).status_code == 404
    assert c.get("/api/llm/models", params={"provider": "ollama"}).json()["models"][0]["vision"] is False
    r = c.put("/api/llm", json={"provider": "ollama", "model": "llama3.2"})
    assert r.status_code == 200 and r.json()["current"] == {"provider": "ollama", "model": "llama3.2"}
    assert c.put("/api/llm", json={"provider": "openai", "model": ""}).status_code == 400   # no default: choose one
    assert c.get("/api/health").json()["llm"]["provider"] in config.LLM_PROVIDERS


def test_vision_on_a_text_model_works_from_frame_descriptions(open_session, use, fake_ollama):
    """Fixture keyframes carry descriptions, not pixels, so a text-only model can still analyse them;
    the answer is schema-checked and tagged with the provider + model that produced it."""
    use("ollama", "llama3.2")
    good = {"hook_strength": "weak", "hook_reason": "r", "product_visible": False, "product_first_seen_s": None,
            "brand_match": "no", "brand_match_reason": "r", "cta_present": False, "cta_t_s": None,
            "face_on_screen": False, "safety_issues": [], "frames": [{"t_s": 0, "note": "n", "on_screen_text": None}]}
    o = fake_ollama(caps=("completion",), reply=json.dumps(good))
    pre = {"duration_s": 10, "frames": [{"frame_id": "kf00", "t_s": 0, "scene_description": "a desk"}], "transcript": []}
    f = vision.analyse("v1", {"brand": "Acme"}, pre)
    assert f["frames"][0]["frame_id"] == "kf00"
    assert f["_usage"]["provider"] == "ollama" and f["_usage"]["model"] == "llama3.2"
    assert "images" not in o.calls[-1][1]["messages"][1]
