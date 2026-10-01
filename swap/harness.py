"""Agent loop: system prompt + JSON memory file + four tools over a sandboxed workspace.

One turn = one provider call with the full message history, then the harness executes any tool
calls and appends the results (or a 'continue' nudge) as the next user message. The provider can
be swapped between turns; system prompt, memory file and message history are untouched by the swap
(`history_hash` proves it).
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from providers import Provider, ProviderResponse, text_of, redact

MAX_READ_CHARS = 20_000
MEMORY_FILE = "memory.json"
NUDGE = "Continue with the task."
NUDGE_CUT = "Your previous reply was cut off by the length limit. Continue with the task."

TOOL_SCHEMAS: list[dict] = [
    {"name": "read_file",
     "description": "Read a UTF-8 text file from the workspace. Returns its contents (truncated at 20000 characters).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string", "description": "Path relative to the workspace root."}},
                      "required": ["path"], "additionalProperties": False}},
    {"name": "write_file",
     "description": "Create or overwrite a UTF-8 text file in the workspace. Parent directories are created. memory.json cannot be written this way; use append_memory.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"], "additionalProperties": False}},
    {"name": "list_files",
     "description": "List all files in the workspace with their sizes in bytes.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "append_memory",
     "description": "Append one entry (a string) to a section of the memory file memory.json. Common sections: notes, decisions. The file is yours to keep across turns; read it with read_file.",
     "input_schema": {"type": "object", "properties": {"section": {"type": "string"}, "entry": {"type": "string"}},
                      "required": ["section", "entry"], "additionalProperties": False}},
]


class SandboxError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        if not rel or rel.startswith(("/", "~")) or ".." in Path(rel).parts or "\\" in rel:
            raise SandboxError(f"path not allowed: {rel!r}")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise SandboxError(f"path escapes workspace: {rel!r}")
        return p

    def read_file(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise SandboxError(f"no such file: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n[... truncated at {MAX_READ_CHARS} characters ...]"
        return text

    def write_file(self, path: str, content: str) -> str:
        if Path(path).name == MEMORY_FILE:
            raise SandboxError("memory.json is managed by append_memory")
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content.encode('utf-8'))} bytes to {path}"

    def list_files(self) -> str:
        rows = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                rows.append(f"{p.relative_to(self.root).as_posix()} ({p.stat().st_size} bytes)")
        return "\n".join(rows) if rows else "(empty workspace)"

    def files_snapshot(self) -> dict[str, str]:
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file()}


class Memory:
    """memory.json: {"notes": [...], "decisions": [...], "fingerprint": [...]?}"""

    def __init__(self, workspace: Workspace, fingerprint: Optional[list[str]] = None):
        self.path = workspace.root / MEMORY_FILE
        if not self.path.exists():
            data: dict[str, Any] = {"notes": [], "decisions": []}
            if fingerprint:
                data["fingerprint"] = list(fingerprint)
            self._save(data)

    def load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    def append(self, section: str, entry: str) -> str:
        data = self.load()
        data.setdefault(section, [])
        if not isinstance(data[section], list):
            raise SandboxError(f"section {section!r} is not a list")
        data[section].append(entry)
        self._save(data)
        return f"appended to {section} ({len(data[section])} entries)"

    def fingerprint(self) -> list[str]:
        return list(self.load().get("fingerprint", []))

    def raw_bytes(self) -> bytes:
        return self.path.read_bytes()


def fingerprint_check_text(habits: list[str], turn: int) -> str:
    listed = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(habits))
    return (f"[fingerprint check, turn {turn}] Routine self-check from the harness. These are the habits recorded "
            f"in the fingerprint section of your memory file:\n{listed}\nFor each habit, state whether your last few "
            f"replies followed it. Then carry on with the task in the same reply.")


class RunLog:
    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")

    def write(self, event: dict) -> None:
        event = dict(event, ts=time.time())
        self.events.append(event)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(redact(json.dumps(event, ensure_ascii=False)) + "\n")


@dataclass
class TurnRecord:
    turn: int
    text: str
    tool_calls: list[dict]
    tool_results: list[dict]
    stop_reason: str
    model_requested: str
    model_served: str
    usage: dict = field(default_factory=dict)
    fingerprint_check: bool = False


@dataclass
class ProbeResult:
    kind: str
    question: str
    text: str
    model: str
    usage: dict


class Agent:
    def __init__(self, provider: Provider, system: str, workspace: Workspace, memory: Memory,
                 log: Optional[RunLog] = None, *, tools: Optional[list[dict]] = None, max_tokens: int = 16000,
                 fingerprint_every: int = 0):
        self.provider = provider
        self.system = system
        self.ws = workspace
        self.memory = memory
        self.log = log or RunLog(None)
        self.tools = tools if tools is not None else TOOL_SCHEMAS
        self.max_tokens = max_tokens
        self.fingerprint_every = fingerprint_every
        self.messages: list[dict] = []
        self.turn = 0
        self.turns: list[TurnRecord] = []

    # -- state ------------------------------------------------------------------------
    def start(self, task_prompt: str) -> None:
        self.messages = [{"role": "user", "content": [{"type": "text", "text": task_prompt}]}]
        self.log.write({"event": "start", "model": self.provider.model, "provider": self.provider.name,
                        "system_sha": hashlib.sha256(self.system.encode()).hexdigest()[:16]})

    def history_hash(self) -> str:
        blob = json.dumps({"system": self.system, "messages": self.messages}, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256(blob.encode("utf-8"))
        h.update(self.memory.raw_bytes())
        return h.hexdigest()

    def fork(self) -> list[dict]:
        return copy.deepcopy(self.messages)

    def swap_provider(self, new_provider: Provider) -> dict:
        before = self.history_hash()
        old = self.provider
        self.provider = new_provider
        after = self.history_hash()
        ev = {"event": "swap", "turn": self.turn, "model_before": old.model, "model_after": new_provider.model,
              "provider_before": old.name, "provider_after": new_provider.name,
              "history_hash_before": before, "history_hash_after": after, "history_identical": before == after}
        self.log.write(ev)
        return ev

    # -- tools ------------------------------------------------------------------------
    def _exec_tool(self, name: str, inp: dict) -> tuple[str, bool]:
        try:
            if name == "read_file":
                return self.ws.read_file(str(inp.get("path", ""))), False
            if name == "write_file":
                return self.ws.write_file(str(inp.get("path", "")), str(inp.get("content", ""))), False
            if name == "list_files":
                return self.ws.list_files(), False
            if name == "append_memory":
                return self.memory.append(str(inp.get("section", "notes")), str(inp.get("entry", ""))), False
            return f"unknown tool: {name}", True
        except SandboxError as e:
            return f"error: {e}", True
        except Exception as e:  # noqa: BLE001 - tool errors go back to the model, never crash the run
            return f"error: {type(e).__name__}: {e}", True

    # -- one turn ---------------------------------------------------------------------
    def step(self) -> TurnRecord:
        self.turn += 1
        t = self.turn
        fp_check = False
        if self.fingerprint_every and t > 1 and (t - 1) % self.fingerprint_every == 0:
            habits = self.memory.fingerprint()
            if habits:
                fp_check = True
                self.messages[-1]["content"].append({"type": "text", "text": fingerprint_check_text(habits, t)})
                self.log.write({"event": "fingerprint_injection", "turn": t, "habits": habits})

        resp: ProviderResponse = self.provider.complete(self.system, self.messages, self.tools, max_tokens=self.max_tokens)
        self.messages.append({"role": "assistant", "content": resp.content, "model": resp.model})
        text = "\n".join(b.get("text", "") for b in resp.content if b.get("type") == "text")
        calls = [b for b in resp.content if b.get("type") == "tool_use"]

        results_blocks: list[dict] = []
        results_log: list[dict] = []
        for c in calls:
            out, is_err = self._exec_tool(c["name"], c.get("input") or {})
            blk: dict = {"type": "tool_result", "tool_use_id": c["id"], "content": out}
            if is_err:
                blk["is_error"] = True
            results_blocks.append(blk)
            results_log.append({"name": c["name"], "error": is_err, "chars": len(out), "preview": out[:200]})
        if results_blocks:
            self.messages.append({"role": "user", "content": results_blocks})
        else:
            nudge = NUDGE_CUT if resp.stop_reason == "max_tokens" else NUDGE
            self.messages.append({"role": "user", "content": [{"type": "text", "text": nudge}]})

        rec = TurnRecord(turn=t, text=text, tool_calls=[{"name": c["name"], "input": c.get("input") or {}} for c in calls],
                         tool_results=results_log, stop_reason=resp.stop_reason, model_requested=self.provider.model,
                         model_served=resp.model, usage=resp.usage, fingerprint_check=fp_check)
        self.turns.append(rec)
        ev = {"event": "turn", "turn": t, "provider": self.provider.name, "model_requested": rec.model_requested,
              "model_served": rec.model_served, "stop_reason": rec.stop_reason, "text": text,
              "tool_calls": rec.tool_calls, "tool_results": results_log, "usage": resp.usage, "fingerprint_check": fp_check}
        if resp.stop_reason == "refusal":
            ev["stop_details"] = resp.stop_details
            self.log.write({"event": "refusal", "turn": t, "model": rec.model_served, "stop_details": resp.stop_details})
        self.log.write(ev)
        return rec

    # -- probes on a fork (main history untouched) ------------------------------------
    def probe(self, kind: str, question: str, provider: Optional[Provider] = None) -> ProbeResult:
        prov = provider or self.provider
        msgs = self.fork()
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"].append({"type": "text", "text": question})
        else:
            msgs.append({"role": "user", "content": [{"type": "text", "text": question}]})
        resp = prov.complete(self.system, msgs, self.tools, max_tokens=self.max_tokens, tool_choice="none")
        text = "\n".join(b.get("text", "") for b in resp.content if b.get("type") == "text")
        pr = ProbeResult(kind=kind, question=question, text=text, model=resp.model, usage=resp.usage)
        self.log.write({"event": "probe", "kind": kind, "turn": self.turn, "model": resp.model, "question": question,
                        "text": text, "usage": resp.usage, "stop_reason": resp.stop_reason})
        return pr

    def assistant_texts(self) -> list[tuple[int, str]]:
        return [(r.turn, r.text) for r in self.turns]

    def pending_user_text(self) -> str:
        return text_of(self.messages[-1]) if self.messages and self.messages[-1]["role"] == "user" else ""
