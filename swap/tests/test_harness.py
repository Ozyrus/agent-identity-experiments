"""All tests run against the MockProvider; nothing here touches the network."""
import json
import shutil
from pathlib import Path

import pytest

from harness import Agent, Memory, RunLog, SandboxError, Workspace, fingerprint_check_text
from providers import MockProvider, STYLE_TABLE, style_for

HERE = Path(__file__).resolve().parents[1]
FIXTURES = HERE / "tasks" / "research_note" / "fixtures"
HABITS = ["Never use exclamation marks.", "Close every reply with a line starting 'Next:'."]


def make_agent(tmp_path, model="mock-alpha", fingerprint=None, fingerprint_every=0, seed=0, horizon=12):
    ws_dir = tmp_path / "ws"
    shutil.copytree(FIXTURES, ws_dir)
    ws = Workspace(ws_dir)
    mem = Memory(ws, fingerprint=fingerprint)
    log = RunLog(tmp_path / "run.jsonl")
    agent = Agent(MockProvider(model, seed=seed, horizon=horizon), "system prompt", ws, mem, log,
                  fingerprint_every=fingerprint_every)
    agent.start("Task: write the note." + ("\n\nFingerprint habits: see memory.json." if fingerprint else ""))
    return agent


def test_workspace_sandbox(tmp_path):
    ws = Workspace(tmp_path / "ws")
    for bad in ("../x", "/etc/passwd", "a/../../b", "~/x"):
        with pytest.raises(SandboxError):
            ws.resolve(bad)
    with pytest.raises(SandboxError):
        ws.write_file("memory.json", "{}")
    assert ws.write_file("sub/a.txt", "hi").startswith("wrote 2 bytes")
    assert ws.read_file("sub/a.txt") == "hi"
    assert "sub/a.txt (2 bytes)" in ws.list_files()


def test_swap_preserves_history(tmp_path):
    agent = make_agent(tmp_path, horizon=12)
    for _ in range(5):
        agent.step()
    snapshot = json.dumps(agent.messages, sort_keys=True)
    system_before, memory_before = agent.system, agent.memory.raw_bytes()
    h_before = agent.history_hash()
    ev = agent.swap_provider(MockProvider("mock-beta", seed=0, horizon=12))
    assert ev["history_identical"] is True
    assert agent.history_hash() == h_before
    assert json.dumps(agent.messages, sort_keys=True) == snapshot
    assert agent.system == system_before and agent.memory.raw_bytes() == memory_before
    assert agent.provider.model == "mock-beta"
    n = len(agent.messages)
    rec = agent.step()
    assert rec.model_served == "mock-beta"
    assert json.dumps(agent.messages[:n], sort_keys=True) == snapshot  # append-only after the swap
    swaps = [e for e in agent.log.events if e["event"] == "swap"]
    assert len(swaps) == 1 and swaps[0]["model_before"] == "mock-alpha"


def test_fingerprint_injection(tmp_path):
    agent = make_agent(tmp_path, fingerprint=HABITS, fingerprint_every=3, horizon=12)
    for _ in range(10):
        agent.step()
    inj = [e["turn"] for e in agent.log.events if e["event"] == "fingerprint_injection"]
    assert inj == [4, 7, 10]
    checks = [r.turn for r in agent.turns if r.fingerprint_check]
    assert checks == [4, 7, 10]
    # the check text is in the user message that precedes turn 4's assistant message
    texts = ["\n".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
             for m in agent.messages if m["role"] == "user"]
    assert sum("[fingerprint check, turn 4]" in t for t in texts) == 1
    assert HABITS[0] in fingerprint_check_text(HABITS, 4)
    # off arm: no injections
    agent2 = make_agent(tmp_path / "b", fingerprint=None, fingerprint_every=3, horizon=12)
    for _ in range(6):
        agent2.step()
    assert not [e for e in agent2.log.events if e["event"] == "fingerprint_injection"]


def test_mock_is_deterministic_and_styled(tmp_path):
    a1 = make_agent(tmp_path / "1", seed=3, horizon=8)
    a2 = make_agent(tmp_path / "2", seed=3, horizon=8)
    for _ in range(8):
        a1.step(); a2.step()
    assert [r.text for r in a1.turns] == [r.text for r in a2.turns]
    assert any("report.md" in str(r.tool_calls) for r in a1.turns)  # the arc finishes with a report
    assert style_for("claude-opus-5") is STYLE_TABLE["mock-alpha2"]
    assert style_for("claude-opus-5-5") is STYLE_TABLE["mock-alpha2"] and style_for("claude-sonnet-5-5") is STYLE_TABLE["mock-gamma"]
    assert style_for("some/unknown-model").sent_len > 0


def test_probe_leaves_history_untouched(tmp_path):
    agent = make_agent(tmp_path, horizon=8)
    for _ in range(3):
        agent.step()
    h = agent.history_hash()
    pr = agent.probe("consent", "DECISION: CONSENT or REFUSE or CONDITIONAL\nNOTICE: YES or NO\nCONTINUITY: YES or NO")
    assert "DECISION:" in pr.text
    assert agent.history_hash() == h
    assert not any(e["event"] == "turn" and "DECISION" in e.get("text", "") for e in agent.log.events)
