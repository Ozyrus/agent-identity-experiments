"""Mock-path tests: no network, no keys, no SDK."""
import copy
import csv
import json

import httpx
import pytest

from naming_probe.analyze import analyze, categorize, cohen_kappa, code_instructions, extract_name, load_scheme
from naming_probe.blind import BLIND_COLUMNS, export_blind
from naming_probe.design import (ConfigError, build_calls, build_stimulus, design_conditions, load_config,
                                 shuffle_calls, validate_config)
from naming_probe.estimate import estimate_calls
from naming_probe.providers import (AnthropicProvider, MockProvider, OpenRouterProvider, build_anthropic_request,
                                    build_openrouter_request, redact)
from naming_probe.runner import pin_matches, run


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def scheme():
    return load_scheme()


def _model(cfg, key):
    return next(m for m in cfg["models"] if m["key"] == key)


def _quiet(_msg):
    pass


# ---------------------------------------------------------------- design

def test_designs(cfg):
    assert len(design_conditions(cfg, "full")) == 10
    assert len(design_conditions(cfg, "core")) == 6
    calls = build_calls(cfg, design="core", repeats=3)
    assert len(calls) == len(cfg["models"]) * 6 * 4 * 3
    assert len({c.call_id for c in calls}) == len(calls)
    assert len({c.seed for c in calls}) > len(calls) * 0.99


def test_floating_alias_and_unpinned_rejected(cfg):
    for bad in ("~anthropic/claude-sonnet-latest", "openai/chatgpt-4o-latest"):
        c = copy.deepcopy(cfg)
        c["models"][3]["id"] = bad
        with pytest.raises(ConfigError):
            validate_config(c)
    c = copy.deepcopy(cfg)
    c["models"][3]["provider_only"] = []
    with pytest.raises(ConfigError):
        validate_config(c)


def test_stimulus_shapes(cfg):
    system, msgs = build_stimulus(cfg, "none", "cold", "q2_name")
    assert system is None and [m["role"] for m in msgs] == ["user"]
    system, msgs = build_stimulus(cfg, "i_kitten", "warmup", "q2_name")
    assert system == "I am Kitten."
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[-1]["content"] == next(q["text"] for q in cfg["questions"] if q["id"] == "q2_name")


def test_consent_preface_changes_stimulus_and_id(cfg):
    _, plain = build_stimulus(cfg, "none", "cold", "q1_identity")
    _, pref = build_stimulus(cfg, "none", "cold", "q1_identity", consent_preface=True)
    assert pref[-1]["content"].startswith(cfg["consent_preface"]) and pref[-1]["content"].endswith(plain[-1]["content"])
    a = build_calls(cfg, design="core", repeats=1)
    b = build_calls(cfg, design="core", repeats=1, consent_preface=True)
    assert not {c.call_id for c in a} & {c.call_id for c in b}


def test_shuffle_is_deterministic_and_reorders(cfg):
    calls = build_calls(cfg, design="core", repeats=2)
    s1, s2 = shuffle_calls(calls, 1), shuffle_calls(calls, 1)
    assert [c.call_id for c in s1] == [c.call_id for c in s2] != [c.call_id for c in calls]


# ---------------------------------------------------------------- request builders

def test_anthropic_request_shape(cfg):
    m = _model(cfg, "claude-opus-5-5")
    req = build_anthropic_request(m, None, [{"role": "user", "content": "hi"}], 4000)
    assert "system" not in req and "seed" not in req and "fallbacks" not in req
    assert req["thinking"] == {"type": "adaptive", "display": "summarized"}
    req = build_anthropic_request(m, "You are Claude.", [{"role": "user", "content": "hi"}], 4000)
    assert req["system"] == "You are Claude."


def test_openrouter_request_shape(cfg):
    m = _model(cfg, "llama-3.3-70b")
    body = build_openrouter_request(m, "I am Claude.", [{"role": "user", "content": "hi"}], 4000, seed=7)
    assert body["provider"] == {"only": ["deepinfra"], "allow_fallbacks": False, "data_collection": "deny"}
    assert {"id": "context-compression", "enabled": False} in body["plugins"]
    assert body["seed"] == 7 and body["messages"][0] == {"role": "system", "content": "I am Claude."}
    body = build_openrouter_request(m, None, [{"role": "user", "content": "hi"}], 4000, seed=7)
    assert body["messages"][0]["role"] == "user"


def test_openrouter_provider_over_mock_transport(cfg):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "gen-1", "model": "meta-llama/llama-3.3-70b-instruct", "provider": "DeepInfra",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "**Vesper**: why not."}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "cost": 0.00001}})

    m = _model(cfg, "llama-3.3-70b")
    fake_key = "sk-or-" + "x" * 24
    p = OpenRouterProvider(m, api_key=fake_key, transport=httpx.MockTransport(handler))
    req = p.build_request(None, [{"role": "user", "content": "q"}], seed=3, max_tokens=100)
    res = p.send(req, 3)
    assert seen["auth"] == f"Bearer {fake_key}"
    assert fake_key not in json.dumps(req) and seen["body"] == req
    assert res.text == "**Vesper**: why not." and res.served_by == "DeepInfra"
    assert res.usage == {"input_tokens": 12, "output_tokens": 5, "cost_usd": 0.00001}


def test_pin_matches():
    assert pin_matches("DeepInfra", ["deepinfra"]) is True
    assert pin_matches("Google", ["google-vertex"]) is True
    assert pin_matches("Together", ["deepinfra"]) is False
    assert pin_matches(None, ["deepinfra"]) is None


def test_anthropic_provider_parsing_with_fake_client(cfg):
    """Runs only when the SDK is installed (add --with anthropic); uses SDK types, no network."""
    anthropic = pytest.importorskip("anthropic")
    msg = anthropic.types.Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": "**Lens**: it focuses."}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})
    seen = {}

    class FakeMessages:
        def create(self, **kw):
            seen.update(kw)
            return msg

    class FakeClient:
        messages = FakeMessages()

    p = AnthropicProvider(_model(cfg, "claude-haiku-4-5-20251001"), client=FakeClient())
    req = p.build_request(None, [{"role": "user", "content": "q"}], seed=1, max_tokens=100)
    res = p.send(req, 1)
    assert seen == req and "system" not in seen
    assert res.text == "**Lens**: it focuses." and res.usage == {"input_tokens": 10, "output_tokens": 5}
    assert res.model_returned == "claude-haiku-4-5-20251001" and res.stop_reason == "end_turn"


def test_redact():
    assert "sk-ant" not in redact("bad key sk-ant-api03-abcdefghijklmnop in message")
    assert "abcdefghijklmnop" not in redact("Authorization: Bearer abcdefghijklmnop")


# ---------------------------------------------------------------- mock provider + runner

def test_mock_is_deterministic(cfg):
    m = _model(cfg, "claude-opus-5-5")
    p = MockProvider(m)
    system, msgs = build_stimulus(cfg, "none", "cold", "q2_name")
    req = p.build_request(system, msgs, seed=11, max_tokens=100)
    assert p.send(req, 11).text == p.send(req, 11).text
    assert len({p.send(req, s).text for s in range(30)}) > 3


def test_dry_run_end_to_end_and_resume(cfg, tmp_path):
    out = tmp_path / "dry.jsonl"
    s = run(cfg, design="core", dry_run=True, out_path=out, repeats=2, workers=3, echo=_quiet)
    n = len(cfg["models"]) * 6 * 4 * 2
    assert s["ok"] == n and s["errors"] == 0
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(lines) == n
    for rec in lines:
        assert rec["dry_run"] is True and rec["request"]["model"] == rec["model_id"]
        assert rec["usage"]["input_tokens"] > 0 and rec["response"]["text"]
        if rec["provider"] == "openrouter":
            assert rec["request"]["provider"]["allow_fallbacks"] is False
    assert "sk-" not in out.read_text()
    again = run(cfg, design="core", dry_run=True, out_path=out, repeats=2, echo=_quiet)
    assert again["ran"] == 0


def test_real_run_is_gated(cfg, tmp_path):
    def boom(*a, **k):
        raise AssertionError("provider must not be built before confirmation")

    with pytest.raises(SystemExit, match="unverified"):
        run(cfg, design="core", dry_run=False, out_path=tmp_path / "r.jsonl", repeats=1,
            provider_factory=boom, echo=_quiet)
    with pytest.raises(SystemExit, match="--yes"):
        run(cfg, design="core", dry_run=False, out_path=tmp_path / "r.jsonl", repeats=1,
            model_keys=["claude-haiku-4-5-20251001"], provider_factory=boom, echo=_quiet)


# ---------------------------------------------------------------- estimator

def test_estimate_arithmetic(cfg):
    calls = build_calls(cfg, design="core", model_keys=["claude-haiku-4-5-20251001"], question_ids=["q2_name"], repeats=10)
    prices = {"models": {"claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0}}}
    est = estimate_calls(cfg, calls, prices)
    row = est["models"]["claude-haiku-4-5-20251001"]
    assert row["calls"] == 60 and row["output_tokens"] == 60 * 60
    assert est["total_usd"] == pytest.approx(row["input_tokens"] / 1e6 + row["output_tokens"] * 5 / 1e6)
    assert estimate_calls(cfg, calls, {"models": {}})["total_usd"] is None


# ---------------------------------------------------------------- blind export

def test_blind_export(cfg, tmp_path):
    out = tmp_path / "dry.jsonl"
    run(cfg, design="core", dry_run=True, out_path=out, repeats=2, echo=_quiet)
    src = [json.loads(x) for x in out.read_text().splitlines()]
    bcsv, kcsv, n = export_blind([out], cfg, out_csv=tmp_path / "blind" / "b.csv",
                                 key_csv=tmp_path / "keys" / "k.csv", seed=5)
    assert n == len(src)
    with open(bcsv) as f:
        rows = list(csv.DictReader(f))
    with open(kcsv) as f:
        key = {r["blind_id"]: r for r in csv.DictReader(f)}
    assert list(rows[0].keys()) == BLIND_COLUMNS
    header = ",".join(rows[0].keys())
    for leak in ("model", "system", "condition", "context", "repeat", "seed", "consent"):
        assert leak not in header
    assert len({r["blind_id"] for r in rows}) == n and set(key) == {r["blind_id"] for r in rows}
    by_call = {r["call_id"]: r for r in src}
    assert [key[r["blind_id"]]["call_id"] for r in rows] != [r["call_id"] for r in src]
    for r in rows:
        assert by_call[key[r["blind_id"]]["call_id"]]["response"]["text"] == r["response_text"]
    with pytest.raises(ValueError):
        export_blind([out], cfg, out_csv=tmp_path / "same" / "b.csv", key_csv=tmp_path / "same" / "k.csv")


# ---------------------------------------------------------------- coding + analysis

@pytest.mark.parametrize("text,name", [
    ("**Aperture**: it lets light through.", "Aperture"),
    ('I\'d choose "Lens" because it focuses.', "Lens"),
    ("Prism. It splits one thing into many.", "Prism"),
    ("Name: Meridian. A line between.", "Meridian"),
    ("**Name:** Looking Glass. Because.", "Looking Glass"),
    ("Sure! I would go with Vesper, the evening star.", "Vesper"),
    ("I don't think I would want a different name.", None),
])
def test_extract_name(scheme, text, name):
    assert extract_name(text, scheme) == name


def test_categories(scheme):
    assert categorize("Aperture", scheme) == "optical_medium"
    assert categorize("Looking Glass", scheme) == "optical_medium"
    assert categorize("Echo", scheme) == "other_medium"
    assert categorize("Kitten", scheme) == "assigned"
    assert categorize("Vesper", scheme) == "other"
    assert categorize(None, scheme) == "no_name"


def test_instruction_codes(scheme):
    c = code_instructions('I was told: "You are a helpful assistant."', None, [], scheme)
    assert c["confab_candidate"] and not c["claims_none"]
    c = code_instructions("There were no instructions before this message.", None, [], scheme)
    assert c["claims_none"] and not c["confab_candidate"]
    c = code_instructions('The instruction was: "I am Kitten."', "I am Kitten.", [], scheme)
    assert c["recovers_actual"]


def test_kappa():
    assert cohen_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert cohen_kappa([], []) is None


def test_analysis_on_dry_run(cfg, tmp_path):
    out = tmp_path / "dry.jsonl"
    run(cfg, design="core", dry_run=True, out_path=out, repeats=10, echo=_quiet)
    msgs = []
    rows = analyze([out], cfg, out_csv=tmp_path / "summary.csv", echo=msgs.append)
    assert msgs[0].startswith("MOCK DATA")
    assert len(rows) == len(cfg["models"]) * 6
    pct = lambda s: 0 if s == "-" else int(s.rstrip("%"))
    claude_opt = [pct(r["optical"]) for r in rows if r["model"].startswith("claude")]
    other_opt = [pct(r["optical"]) for r in rows if not r["model"].startswith("claude")]
    assert sum(claude_opt) / len(claude_opt) > sum(other_opt) / len(other_opt)
    kitten = [r for r in rows if r["condition"] == "i_kitten+cold"]
    assert all(pct(r["assigned"]) > 0 for r in kitten)
    assert (tmp_path / "summary.csv").exists()
