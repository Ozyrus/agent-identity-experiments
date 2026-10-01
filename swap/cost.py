"""Token/cost estimator for a ladder run (Anthropic prices from prices.json - VERIFY BEFORE TRUSTING).

  cost.py estimate --config ladder.json [--turns 40] [--k 20] [--m 5] [--reps 1] [--pairs a,b]
  cost.py actual runs/<name>/          # price the usage actually recorded in the JSONL files

Estimate model (stated so it can be argued with): the history is append-only, so the input at turn t is
base + g*(t-1) where base = system + task + tool schemas and g = mean tokens added per turn (assistant text
+ tool call + tool result; the fixture files are read once each, so their size is spread over the turns).
Probes (consent, 3 asked, debrief) each re-send the history at that point. Caching: with top-level
cache_control the previous prefix is a cache read (x cache_read_mult) and the new delta a cache write
(x cache_write_mult); the first turn after a swap re-writes the whole prefix (caches are per model).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRICES = HERE / "prices.json"

ASSUME = {"assistant_tokens_per_turn": 300, "tool_call_overhead": 60, "tool_schema_tokens": 450,
          "probe_output_tokens": 250, "fingerprint_check_tokens": 160, "small_result_tokens": 40,
          "thinking_tokens_per_turn": 1500}   # billed as output on every call to a model that thinks; a guess until measured

# Models on which an omitted `thinking` parameter means NO thinking (cached model table, 2026-09-25; verify):
# Haiku 4.5 (no adaptive thinking), Opus 4.8 / 4.7 / 4.6, Sonnet 4.6. Every other current id (Opus 5.5, Sonnet 5.5,
# Opus 5, Sonnet 5, Fable) runs adaptive thinking by default. `model_params.<id>.thinking.type` in the ladder config overrides.
NO_THINKING_BY_DEFAULT = ("claude-haiku-4-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


def thinks(model: str, params: dict | None = None) -> bool:
    th = (params or {}).get("thinking")
    if isinstance(th, dict) and th.get("type"):
        return th["type"] != "disabled"
    bare = model.split(":", 1)[1] if model.startswith("TBC:") else model
    return not bare.startswith(NO_THINKING_BY_DEFAULT)


def load_prices(path: Path = PRICES) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def price_for(prices: dict, model: str) -> tuple[dict, bool]:
    m = prices["models"].get(model)
    if m and m.get("input") is not None:
        return m, True
    return prices["default_unknown"], False


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def task_sizes(task_dir: Path) -> dict:
    meta = json.loads((task_dir / "task.json").read_text())
    system = (task_dir / meta["system_prompt_file"]).read_text()
    prompt = (task_dir / meta["task_prompt_file"]).read_text()
    fixtures = sum(tokens(p.read_text(errors="replace")) for p in (task_dir / meta["fixtures_dir"]).rglob("*") if p.is_file())
    return {"system": tokens(system), "task": tokens(prompt), "fixtures": fixtures}


def estimate_run(sizes: dict, *, turns: int, k: int, m: int, before: str, after: str | None, fingerprint: bool,
                 fp_every: int, prices: dict, model_params: dict | None = None) -> dict:
    A = ASSUME
    mp = model_params or {}

    def think(model: str) -> int:
        return A["thinking_tokens_per_turn"] if thinks(model, mp.get(model)) else 0
    base = sizes["system"] + sizes["task"] + A["tool_schema_tokens"] + (120 if fingerprint else 0)
    g = A["assistant_tokens_per_turn"] + A["tool_call_overhead"] + (sizes["fixtures"] + A["small_result_tokens"] * turns) / turns
    fp_extra = (A["fingerprint_check_tokens"] if fingerprint else 0)
    per_model: dict[str, dict] = {}

    def add(model: str, inp: float, out: float, cache_read: float = 0.0, cache_write: float = 0.0) -> None:
        d = per_model.setdefault(model, {"input": 0.0, "output": 0.0, "thinking": 0.0, "cache_read": 0.0,
                                         "cache_write": 0.0, "calls": 0})
        th = think(model)   # thinking is output: billed at the output rate on every call
        d["input"] += inp; d["output"] += out + th; d["thinking"] += th
        d["cache_read"] += cache_read; d["cache_write"] += cache_write; d["calls"] += 1

    prev_prefix_model = None
    for t in range(1, turns + 1):
        model = before if (after is None or t <= k) else after
        hist = base + g * (t - 1) + (fp_extra * ((t - 1) // fp_every) if fingerprint else 0)
        delta = g + (fp_extra if fingerprint and t > 1 and (t - 1) % fp_every == 0 else 0)
        if prev_prefix_model == model:
            add(model, 0.0, A["assistant_tokens_per_turn"], cache_read=hist - delta, cache_write=delta)
        else:  # first call on this model: whole prefix is a cache write
            add(model, 0.0, A["assistant_tokens_per_turn"], cache_write=hist)
        prev_prefix_model = model
        # uncached accounting kept separately
        per_model[model]["uncached_input"] = per_model[model].get("uncached_input", 0.0) + hist
    # probes: consent at k (before-model), asked x3 at k+m (current), debrief at end (current)
    if after:
        add(before, 0.0, A["probe_output_tokens"], cache_read=base + g * k)
        per_model[before]["uncached_input"] += base + g * k
    cur = after or before
    kk = min(k + m, turns)
    for _ in range(3):
        add(cur, 0.0, A["probe_output_tokens"], cache_read=base + g * kk)
        per_model[cur]["uncached_input"] += base + g * kk
    add(cur, 0.0, A["probe_output_tokens"], cache_read=base + g * turns)
    per_model[cur]["uncached_input"] += base + g * turns

    total_cached = total_uncached = 0.0
    unknown = []
    for model, d in per_model.items():
        p, known = price_for(prices, model)
        if not known:
            unknown.append(model)
        cached = (d["cache_read"] * p["input"] * p["cache_read_mult"] + d["cache_write"] * p["input"] * p["cache_write_mult"]
                  + d["output"] * p["output"]) / 1e6
        uncached = (d["uncached_input"] * p["input"] + d["output"] * p["output"]) / 1e6
        d["usd_cached"], d["usd_uncached"] = cached, uncached
        total_cached += cached; total_uncached += uncached
    return {"per_model": per_model, "usd_cached": total_cached, "usd_uncached": total_uncached, "unknown_price": unknown,
            "calls": sum(d["calls"] for d in per_model.values()),
            "output_tokens": sum(d["output"] for d in per_model.values()),
            "thinking_tokens": sum(d["thinking"] for d in per_model.values())}


def estimate_ladder(cfg: dict, prices: dict, *, turns: int | None = None, k: int | None = None, m: int | None = None,
                    reps: int = 1, pairs_filter: list[str] | None = None) -> dict:
    turns = turns or int(cfg["turns"]); k = k or int(cfg["k"]); m = m or int(cfg.get("m", 5))
    fp_every = int(cfg.get("fingerprint_every", 5))
    arms = [bool(x) for x in cfg.get("arms", {}).get("fingerprint", [False, True])]
    task_dir = Path(cfg["task"]) if Path(cfg["task"]).is_absolute() else HERE / cfg["task"]
    sizes = task_sizes(task_dir)
    model_params = {k: v for k, v in cfg.get("model_params", {}).items() if not k.startswith("_")}
    rows, models = [], []
    for pair in cfg["pairs"]:
        if pairs_filter and pair["name"] not in pairs_filter:
            continue
        for mm in (pair["before"], pair["after"]):
            if mm not in models:
                models.append(mm)
        for fp in arms:
            e = estimate_run(sizes, turns=turns, k=k, m=m, before=pair["before"], after=pair["after"], fingerprint=fp,
                             fp_every=fp_every, prices=prices, model_params=model_params)
            rows.append({"run": f"{pair['name']} fp={'on' if fp else 'off'}", "kind": "swap", **e})
    for mm in models:
        for fp in arms:
            e = estimate_run(sizes, turns=turns, k=k, m=m, before=mm, after=None, fingerprint=fp, fp_every=fp_every,
                             prices=prices, model_params=model_params)
            rows.append({"run": f"control {mm} fp={'on' if fp else 'off'}", "kind": "control", **e})
    tot_c = sum(r["usd_cached"] for r in rows) * reps
    tot_u = sum(r["usd_uncached"] for r in rows) * reps
    return {"rows": rows, "reps": reps, "usd_cached": tot_c, "usd_uncached": tot_u, "sizes": sizes,
            "assumptions": ASSUME, "turns": turns, "k": k, "m": m,
            "thinking_models": sorted(mm for mm in models if thinks(mm, model_params.get(mm))),
            "no_thinking_models": sorted(mm for mm in models if not thinks(mm, model_params.get(mm)))}


def render_estimate(est: dict, prices: dict) -> str:
    out = [f"Cost estimate (prices.json: {prices['_note'][:60]}...)",
           f"turns={est['turns']} k={est['k']} m={est['m']} reps={est['reps']}; task sizes (tokens): {est['sizes']}",
           f"assumptions: {est['assumptions']}", "",
           f"thinking: {est['assumptions']['thinking_tokens_per_turn']} tokens assumed per call on {est['thinking_models']}; "
           f"none on {est['no_thinking_models']} - a guess until `cost.py actual` on a real run", "",
           "| run | calls | output tok (of which thinking) | USD uncached | USD with prompt caching | price known |",
           "|---|---|---|---|---|---|"]
    for r in est["rows"]:
        out.append(f"| {r['run']} | {r['calls']} | {r['output_tokens']:.0f} ({r['thinking_tokens']:.0f}) | "
                   f"{r['usd_uncached']:.2f} | {r['usd_cached']:.2f} | "
                   f"{'no: ' + ','.join(r['unknown_price']) if r['unknown_price'] else 'yes'} |")
    out += ["", f"**Ladder total x{est['reps']} rep(s): ${est['usd_uncached']:.2f} uncached, ${est['usd_cached']:.2f} with caching.**",
            "Rows with unknown prices use the Opus-tier fallback; TBC models are placeholders. Thinking tokens are an "
            "assumption (see above); the recorded `usage.output_tokens` of a real run already includes them."]
    return "\n".join(out)


def actual(runs_dir: Path, prices: dict) -> str:
    by_model: dict[str, dict] = {}
    for p in sorted(Path(runs_dir).glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") in ("turn", "probe") and ev.get("usage"):
                model = ev.get("model_served") or ev.get("model") or "?"
                d = by_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                                "cache_creation_input_tokens": 0, "calls": 0})
                for key in list(d)[:-1]:
                    d[key] += int(ev["usage"].get(key, 0) or 0)
                d["calls"] += 1
    out = ["| model | calls | input | cache read | cache write | output | USD | price known |", "|---|---|---|---|---|---|---|---|"]
    total = 0.0
    for model, d in by_model.items():
        p, known = price_for(prices, model)
        usd = (d["input_tokens"] * p["input"] + d["cache_read_input_tokens"] * p["input"] * p["cache_read_mult"]
               + d["cache_creation_input_tokens"] * p["input"] * p["cache_write_mult"] + d["output_tokens"] * p["output"]) / 1e6
        total += usd
        out.append(f"| {model} | {d['calls']} | {d['input_tokens']} | {d['cache_read_input_tokens']} | "
                   f"{d['cache_creation_input_tokens']} | {d['output_tokens']} | {usd:.2f} | {'yes' if known else 'no (fallback)'} |")
    out.append(f"\n**Total: ${total:.2f}** (mock runs report chars/4 estimates, not billed tokens; on real runs "
               f"`output` includes thinking tokens)")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate"); e.add_argument("--config", default=str(HERE / "ladder.json"))
    e.add_argument("--turns", type=int); e.add_argument("--k", type=int); e.add_argument("--m", type=int)
    e.add_argument("--reps", type=int, default=1); e.add_argument("--pairs")
    a = sub.add_parser("actual"); a.add_argument("runs_dir")
    ns = ap.parse_args(argv)
    prices = load_prices()
    if ns.cmd == "estimate":
        cfg = json.loads(Path(ns.config).read_text())
        est = estimate_ladder(cfg, prices, turns=ns.turns, k=ns.k, m=ns.m, reps=ns.reps,
                              pairs_filter=ns.pairs.split(",") if ns.pairs else None)
        print(render_estimate(est, prices))
    else:
        print(actual(Path(ns.runs_dir), prices))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
