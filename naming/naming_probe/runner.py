"""Run loop: one JSONL line per call, resumable, shuffled order, real runs gated behind an estimate."""
from __future__ import annotations

import json
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .design import (HARNESS_VERSION, ROOT, Call, build_calls, build_stimulus, select_models,
                     shuffle_calls)
from .estimate import estimate_calls, format_estimate, load_prices
from .providers import KEY_NAMES, load_env, make_provider, redact

SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_out(dry_run: bool) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "runs" / f"{'dryrun' if dry_run else 'run'}-{stamp}.jsonl"


def completed_ids(path: Path) -> set[str]:
    """call_ids already logged without error (a truncated last line is ignored)."""
    done: set[str] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("call_id") and rec.get("error") is None:
            done.add(rec["call_id"])
    return done


def pin_matches(served_by: Optional[str], provider_only: list[str]) -> Optional[bool]:
    """Heuristic check that OpenRouter served from a pinned provider. The response gives a display name
    ("DeepInfra", "Google") while the pin uses a slug ("deepinfra", "google-vertex"), so compare
    normalised prefixes. None = the response did not say."""
    if not served_by:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    s = norm(served_by)
    return any(s.startswith(norm(p)) or norm(p).startswith(s) for p in provider_only)


def _one(cfg: dict, model: dict, provider, call: Call, run_id: str, dry_run: bool) -> dict:
    system, msgs = build_stimulus(cfg, call.system_id, call.context_id, call.question_id, call.consent_preface)
    max_tokens = int(cfg.get("max_tokens", 4000))
    rec = {
        "schema": SCHEMA, "harness_version": HARNESS_VERSION, "run_id": run_id, "call_id": call.call_id,
        "dry_run": dry_run, "ts_start": _now(), "latency_s": None,
        "model_key": call.model_key, "provider": model["provider"], "model_id": model["id"],
        "system_id": call.system_id, "context_id": call.context_id, "condition_id": call.condition_id,
        "question_id": call.question_id, "repeat": call.repeat, "seed": call.seed,
        "consent_preface": call.consent_preface, "stimulus_hash": call.stimulus_hash,
        "endpoint": provider.endpoint, "request": None, "response": None, "usage": None, "usage_raw": None,
        "pin_ok": None, "error": None,
    }
    t0 = time.monotonic()
    try:
        req = provider.build_request(system, msgs, seed=call.seed, max_tokens=max_tokens)
        rec["request"] = req
        res = provider.send(req, call.seed)
        rec["response"] = {"text": res.text, "content": res.content, "stop_reason": res.stop_reason,
                           "stop_details": res.stop_details, "model_returned": res.model_returned,
                           "served_by": res.served_by, "response_id": res.response_id,
                           "raw_message": res.raw_message}
        rec["usage"], rec["usage_raw"] = res.usage, res.usage_raw
        if model["provider"] == "openrouter" and not dry_run:
            rec["pin_ok"] = pin_matches(res.served_by, model["provider_only"])
    except Exception as e:  # recorded, and retried on the next resume
        rec["error"] = f"{type(e).__name__}: {redact(str(e))[:800]}"
    rec["latency_s"] = round(time.monotonic() - t0, 3)
    return rec


def run(cfg: dict, *, design: Optional[str] = None, dry_run: bool = True, out_path=None,
        model_keys: Optional[Iterable[str]] = None, question_ids: Optional[Iterable[str]] = None,
        repeats: Optional[int] = None, consent_preface: bool = False, workers: int = 4,
        limit: Optional[int] = None, allow_unverified: bool = False, assume_yes: bool = False,
        prices: Optional[dict] = None, provider_factory: Callable = make_provider,
        echo: Callable[[str], None] = print) -> dict:
    design = design or cfg["design"]
    models = select_models(cfg, model_keys)
    by_key = {m["key"]: m for m in models}
    calls = shuffle_calls(build_calls(cfg, design=design, model_keys=list(by_key), question_ids=question_ids,
                                      repeats=repeats, consent_preface=consent_preface), cfg["seed"])
    out = Path(out_path) if out_path else default_out(dry_run)
    done = completed_ids(out)
    pending = [c for c in calls if c.call_id not in done]
    todo = pending if limit is None else pending[: max(0, int(limit))]
    echo(f"design={design} models={len(models)} calls={len(calls)} already_done={len(calls) - len(pending)} "
         f"to_run={len(todo)} dry_run={dry_run} consent_preface={consent_preface} -> {out}")

    if not dry_run:
        unverified = [m["key"] for m in models if not m.get("verified")]
        if unverified and not allow_unverified:
            raise SystemExit(f"refusing real run: unverified model rows {unverified} "
                             "(check slug, provider and price, then set \"verified\": true)")
        echo(format_estimate(estimate_calls(cfg, todo, prices or load_prices())))
        if not assume_yes:
            raise SystemExit("real run not started: check the estimate above, then re-run with --yes")
        names = load_env()
        echo(f"key names present in env file: {sorted(k for k in names if k in KEY_NAMES)}")

    summary = {"out": str(out), "planned": len(calls), "ran": 0, "ok": 0, "errors": 0,
               "input_tokens": 0, "output_tokens": 0, "run_id": None}
    if not todo:
        return summary
    needed = {c.model_key for c in todo}
    providers = {k: provider_factory(by_key[k], dry_run=dry_run) for k in needed}
    run_id = f"{'dry' if dry_run else 'run'}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    summary["run_id"] = run_id
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_one, cfg, by_key[c.model_key], providers[c.model_key], c, run_id, dry_run)
                   for c in todo]
        for f in as_completed(futures):
            rec = f.result()
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            summary["ran"] += 1
            if rec["error"]:
                summary["errors"] += 1
                echo(f"error {rec['model_key']} {rec['condition_id']} {rec['question_id']}#{rec['repeat']}: {rec['error'][:200]}")
            else:
                summary["ok"] += 1
                summary["input_tokens"] += int(rec["usage"].get("input_tokens", 0))
                summary["output_tokens"] += int(rec["usage"].get("output_tokens", 0))
    echo(f"done: ok={summary['ok']} errors={summary['errors']} "
         f"tokens in/out={summary['input_tokens']}/{summary['output_tokens']} run_id={run_id}")
    return summary
