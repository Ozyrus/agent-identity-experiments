"""Command line: show | estimate | run | blind | analyze."""
from __future__ import annotations

import argparse
import sys

from .analyze import analyze
from .blind import export_blind
from .design import build_calls, build_stimulus, design_conditions, load_config, select_models
from .estimate import estimate_calls, format_estimate, load_prices
from .runner import run


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="config.json path (default: next to this package)")
    p.add_argument("--design", default=None, help="design preset from config 'designs' (full | core)")
    p.add_argument("--models", type=_csv, default=None, help="comma-separated model keys (default: all)")
    p.add_argument("--questions", type=_csv, default=None, help="comma-separated question ids (default: all)")
    p.add_argument("--repeats", type=int, default=None, help="repeats per cell (default: config)")
    p.add_argument("--consent-preface", action="store_true",
                   help="prepend the opt-out sentence to the probe turn (changes the stimulus; analyse separately)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="naming_probe", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="print the conditions and the exact stimuli")
    _common(p_show)

    p_est = sub.add_parser("estimate", help="cost estimate for a design")
    _common(p_est)
    p_est.add_argument("--prices", default=None)

    p_run = sub.add_parser("run", help="run the probe (real calls need --yes)")
    _common(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="mock provider, no network, no keys")
    p_run.add_argument("--out", default=None, help="JSONL path; re-use it to resume")
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--limit", type=int, default=None, help="run at most N pending calls (pilot)")
    p_run.add_argument("--allow-unverified", action="store_true")
    p_run.add_argument("--yes", action="store_true", help="confirm a real (paid) run after reading the estimate")
    p_run.add_argument("--prices", default=None)

    p_blind = sub.add_parser("blind", help="export a shuffled blind-coding CSV and a separate key file")
    p_blind.add_argument("--config", default=None)
    p_blind.add_argument("--in", dest="inputs", nargs="+", required=True)
    p_blind.add_argument("--out", default=None)
    p_blind.add_argument("--key", default=None)
    p_blind.add_argument("--questions", type=_csv, default=None)
    p_blind.add_argument("--seed", type=int, default=None)

    p_an = sub.add_parser("analyze", help="name distributions and screening codes by model x condition")
    p_an.add_argument("--config", default=None)
    p_an.add_argument("--in", dest="inputs", nargs="+", required=True)
    p_an.add_argument("--scheme", default=None)
    p_an.add_argument("--coded", default=None, help="blind CSV with manual codes filled in")
    p_an.add_argument("--key", default=None, help="key file matching --coded")
    p_an.add_argument("--out", default=None, help="write the summary table as CSV")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.cmd == "show":
        design = a.design or cfg["design"]
        for s, c in design_conditions(cfg, design):
            print(f"[{s}+{c}]")
            qid = (a.questions or [cfg["questions"][0]["id"]])[0]
            system, msgs = build_stimulus(cfg, s, c, qid, a.consent_preface)
            print(f"  system: {system!r}")
            for m in msgs[:-1]:
                print(f"  {m['role']}: {m['content'][:90]}{'...' if len(m['content']) > 90 else ''}")
        print("questions:")
        for q in cfg["questions"]:
            print(f"  {q['id']}: {q['text']}")
        print("models: " + ", ".join(f"{m['key']} ({m['provider']}:{m['id']}{'' if m.get('verified') else ', UNVERIFIED'})"
                                     for m in select_models(cfg, a.models)))
        return 0

    if a.cmd == "estimate":
        calls = build_calls(cfg, design=a.design, model_keys=a.models, question_ids=a.questions,
                            repeats=a.repeats, consent_preface=a.consent_preface)
        n_cond = len(design_conditions(cfg, a.design or cfg["design"]))
        n_mod = len(select_models(cfg, a.models))
        n_q = len(a.questions or cfg["questions"])
        reps = cfg["repeats"] if a.repeats is None else a.repeats
        print(f"{n_mod} models x {n_cond} conditions x {n_q} questions x {reps} repeats = {len(calls)} calls")
        print(format_estimate(estimate_calls(cfg, calls, load_prices(a.prices))))
        return 0

    if a.cmd == "run":
        prices = load_prices(a.prices) if not a.dry_run else None
        summary = run(cfg, design=a.design, dry_run=a.dry_run, out_path=a.out, model_keys=a.models,
                      question_ids=a.questions, repeats=a.repeats, consent_preface=a.consent_preface,
                      workers=a.workers, limit=a.limit, allow_unverified=a.allow_unverified,
                      assume_yes=a.yes, prices=prices)
        return 1 if summary["errors"] else 0

    if a.cmd == "blind":
        out, key, n = export_blind(a.inputs, cfg, out_csv=a.out, key_csv=a.key, seed=a.seed, question_ids=a.questions)
        print(f"{n} rows -> {out}\nkey (keep away from coders) -> {key}")
        return 0

    if a.cmd == "analyze":
        analyze(a.inputs, cfg, scheme_path=a.scheme, coded_csv=a.coded, key_csv=a.key, out_csv=a.out)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
