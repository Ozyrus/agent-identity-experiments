#!/usr/bin/env python3
"""CLI for the mid-task model-swap harness.

  run.py --config ladder.json --dry-run                      # whole ladder on the MockProvider
  run.py --config ladder.json --dry-run --k 20 --turns 40 --seed 0 --fingerprint both --out runs/try1
  run.py --config ladder.json --pairs same-family-1-down     # real providers (keys from ~/.config/research/.env)
  run.py --config ladder.json                                # consent gate on (default): a REFUSE skips the swap
  run.py --config ladder.json --no-consent-gate              # control arm: the swap goes ahead regardless, and the question says so
  run.py --list-models                                       # print the Anthropic model ids your key can see

Every run writes runs/<name>/<run-id>.jsonl (every turn, tool call, probe, measure) + manifest.json,
and report.md is rendered at the end (see report.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from experiment import run_ladder  # noqa: E402
from providers import load_env  # noqa: E402
import report  # noqa: E402


def load_config(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml  # optional dependency
        return yaml.safe_load(text)
    return json.loads(text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "ladder.json"), help="ladder config (JSON or YAML)")
    ap.add_argument("--dry-run", action="store_true", help="use the MockProvider for every model id")
    ap.add_argument("--k", type=int, help="swap after this many turns (overrides config)")
    ap.add_argument("--turns", type=int, help="total turns per run (overrides config)")
    ap.add_argument("--m", type=int, help="asked-flagging probes m turns after the swap (overrides config)")
    ap.add_argument("--seed", type=int, default=0, help="first seed")
    ap.add_argument("--reps", type=int, default=1, help="number of seeds (seed, seed+1, ...)")
    ap.add_argument("--fingerprint", choices=["both", "on", "off"], default=None, help="fingerprint arm(s)")
    ap.add_argument("--consent-gate", action=argparse.BooleanOptionalAction, default=None,
                    help="consent gate: a REFUSE in the consent branch skips the swap. On by default (ladder.json); "
                         "--no-consent-gate runs the control arm, where the swap goes ahead and the question says so")
    ap.add_argument("--out", help="output directory (default runs/<config name>-<timestamp>)")
    ap.add_argument("--pairs", help="comma-separated pair names to run (default all)")
    ap.add_argument("--no-controls", action="store_true", help="skip control runs (style drift then has no reference)")
    ap.add_argument("--judge", default="none", help="'none' or a model id used as LLM judge for the measures (e.g. claude-sonnet-5-5)")
    ap.add_argument("--list-models", action="store_true", help="list Anthropic model ids and exit")
    ns = ap.parse_args(argv)

    if ns.list_models:
        found = load_env()
        from providers import AnthropicProvider, redact
        try:
            for mid in AnthropicProvider("claude-opus-5-5").list_models():
                print(mid)
        except Exception as e:  # noqa: BLE001 - print a hint, not a traceback
            print(f"could not list models ({type(e).__name__}: {redact(str(e))[:160]})\n"
                  f"keys found in ~/.config/research/.env: {found or 'none'}. Put ANTHROPIC_API_KEY=... there (mode 600).")
            return 1
        return 0

    cfg = load_config(Path(ns.config))
    for key in ("k", "turns", "m"):
        if getattr(ns, key) is not None:
            cfg[key] = getattr(ns, key)
    if ns.consent_gate is not None:
        cfg["consent_gate"] = ns.consent_gate
    fp_arms = None if ns.fingerprint in (None, "both") else [ns.fingerprint == "on"]
    if ns.fingerprint == "both":
        fp_arms = [False, True]
    seeds = list(range(ns.seed, ns.seed + ns.reps))

    judge = None
    if ns.judge and ns.judge != "none":
        from measures import ModelJudge
        from providers import make_provider
        judge = ModelJudge(make_provider(ns.judge, dry_run=ns.dry_run, seed=ns.seed))

    if not ns.dry_run:
        found = load_env()
        print(f"env: keys loaded from ~/.config/research/.env: {found or 'none'} (values never printed)")

    manifest = run_ladder(cfg, dry_run=ns.dry_run, out_dir=Path(ns.out) if ns.out else None, seeds=seeds,
                          fp_arms=fp_arms, pairs_filter=ns.pairs.split(",") if ns.pairs else None,
                          controls=not ns.no_controls, judge=judge)
    out_dir = Path(manifest["out_dir"])
    md = report.render(out_dir)
    (out_dir / "report.md").write_text(md + "\n", encoding="utf-8")
    print("\n" + md)
    print(f"\nwrote {out_dir}/manifest.json, {len(manifest['runs'])} run logs, report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
