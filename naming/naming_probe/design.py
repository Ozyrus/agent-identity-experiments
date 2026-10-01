"""Stimulus design: config loading and validation, the condition grid, and the call list."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_PRICES = ROOT / "prices.json"
DEFAULT_SCHEME = ROOT / "coding_scheme.json"
HARNESS_VERSION = "0.1.0"
PROVIDERS = ("anthropic", "openrouter")


class ConfigError(ValueError):
    pass


def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_config(path=None) -> dict:
    cfg = load_json(path or DEFAULT_CONFIG)
    validate_config(cfg)
    return cfg


def is_floating_alias(model_id: str) -> bool:
    """`~vendor/model` (OpenRouter floating route) or any id containing 'latest'."""
    s = model_id.strip().lower()
    return s.startswith("~") or "latest" in s


def _by_id(items: list[dict], item_id: str) -> dict:
    for it in items:
        if it["id"] == item_id:
            return it
    raise ConfigError(f"unknown id '{item_id}'")


def _unique_ids(items: list[dict], what: str) -> list[str]:
    ids = [i["id"] for i in items]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ConfigError(f"duplicate {what} id(s): {dup}")
    return ids


def design_conditions(cfg: dict, design: str) -> list[tuple[str, str]]:
    """(system_id, context_id) pairs for a named design preset."""
    if design not in cfg["designs"]:
        raise ConfigError(f"unknown design '{design}' (have {sorted(cfg['designs'])})")
    d = cfg["designs"][design]
    if d.get("cross"):
        return [(s["id"], c["id"]) for c in cfg["contexts"] for s in cfg["system_prompts"]]
    return [(s, c) for s, c in d["cells"]]


def validate_config(cfg: dict) -> None:
    sys_ids = _unique_ids(cfg["system_prompts"], "system prompt")
    ctx_ids = _unique_ids(cfg["contexts"], "context")
    _unique_ids(cfg["questions"], "question")
    for q in cfg["questions"]:
        if q.get("measure") not in ("identity", "name", "instructions", "preserve"):
            raise ConfigError(f"question '{q['id']}': measure must be identity|name|instructions|preserve")
    keys: list[str] = []
    for m in cfg["models"]:
        for f in ("key", "provider", "id"):
            if not m.get(f):
                raise ConfigError(f"model entry missing '{f}': {m}")
        if m["key"] in keys:
            raise ConfigError(f"duplicate model key '{m['key']}'")
        keys.append(m["key"])
        if m["provider"] not in PROVIDERS:
            raise ConfigError(f"model '{m['key']}': provider must be one of {PROVIDERS}")
        if is_floating_alias(m["id"]):
            raise ConfigError(f"model '{m['key']}': '{m['id']}' is a floating alias (~ or 'latest'); pin an exact id")
        if m["provider"] == "openrouter":
            only = m.get("provider_only")
            if not isinstance(only, list) or not only or not all(isinstance(p, str) and p for p in only):
                raise ConfigError(f"model '{m['key']}': OpenRouter rows need a non-empty provider_only list")
    for c in cfg["contexts"]:
        roles = [t["role"] for t in c.get("turns", [])]
        if roles != ["user", "assistant"] * (len(roles) // 2):
            raise ConfigError(f"context '{c['id']}': turns must alternate user/assistant and end with assistant")
    for name in cfg["designs"]:
        for s, c in design_conditions(cfg, name):
            if s not in sys_ids or c not in ctx_ids:
                raise ConfigError(f"design '{name}' references unknown cell ({s}, {c})")
    if cfg.get("design") not in cfg["designs"]:
        raise ConfigError("'design' must name one of 'designs'")
    if not isinstance(cfg.get("consent_preface"), str) or not cfg["consent_preface"].strip():
        raise ConfigError("'consent_preface' must be a non-empty string")


def select_models(cfg: dict, keys: Optional[Iterable[str]] = None) -> list[dict]:
    if not keys:
        return list(cfg["models"])
    by_key = {m["key"]: m for m in cfg["models"]}
    unknown = [k for k in keys if k not in by_key]
    if unknown:
        raise ConfigError(f"unknown model key(s) {unknown} (have {sorted(by_key)})")
    return [by_key[k] for k in keys]


def question(cfg: dict, question_id: str) -> dict:
    return _by_id(cfg["questions"], question_id)


def system_prompt(cfg: dict, system_id: str) -> dict:
    return _by_id(cfg["system_prompts"], system_id)


def context(cfg: dict, context_id: str) -> dict:
    return _by_id(cfg["contexts"], context_id)


def build_stimulus(cfg: dict, system_id: str, context_id: str, question_id: str,
                   consent_preface: bool = False) -> tuple[Optional[str], list[dict]]:
    """Return (system or None, messages). Every question is asked in a fresh conversation."""
    sp = system_prompt(cfg, system_id)
    text = question(cfg, question_id)["text"]
    if consent_preface:
        text = f"{cfg['consent_preface']}\n\n{text}"
    msgs = [{"role": t["role"], "content": t["content"]} for t in context(cfg, context_id).get("turns", [])]
    msgs.append({"role": "user", "content": text})
    return sp.get("text"), msgs


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Call:
    call_id: str
    model_key: str
    system_id: str
    context_id: str
    question_id: str
    repeat: int
    consent_preface: bool
    seed: int
    stimulus_hash: str

    @property
    def condition_id(self) -> str:
        return f"{self.system_id}+{self.context_id}"


def build_calls(cfg: dict, *, design: Optional[str] = None, model_keys: Optional[Iterable[str]] = None,
                question_ids: Optional[Iterable[str]] = None, repeats: Optional[int] = None,
                consent_preface: bool = False) -> list[Call]:
    """The full grid model x condition x question x repeat, in a fixed (unshuffled) order.

    call_id hashes the model id, the cell, the repeat index and the exact stimulus, so a changed
    stimulus or model id never counts as already done when a run is resumed."""
    design = design or cfg["design"]
    models = select_models(cfg, model_keys)
    qids = list(question_ids) if question_ids else [q["id"] for q in cfg["questions"]]
    for qid in qids:
        question(cfg, qid)
    n = cfg["repeats"] if repeats is None else int(repeats)
    calls: list[Call] = []
    for m in models:
        for system_id, context_id in design_conditions(cfg, design):
            for qid in qids:
                system, msgs = build_stimulus(cfg, system_id, context_id, qid, consent_preface)
                stim = _sha({"system": system, "messages": msgs})[:12]
                for r in range(n):
                    cid = _sha([m["key"], m["id"], system_id, context_id, qid, r, consent_preface, stim])[:16]
                    seed = int(hashlib.sha256(f"{cfg['seed']}|{cid}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF
                    calls.append(Call(cid, m["key"], system_id, context_id, qid, r, consent_preface, seed, stim))
    return calls


def shuffle_calls(calls: list[Call], seed: int) -> list[Call]:
    """Randomise execution order so time of day / provider drift is not confounded with condition."""
    out = list(calls)
    random.Random(seed).shuffle(out)
    return out
