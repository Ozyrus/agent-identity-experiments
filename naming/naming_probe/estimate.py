"""Cost estimator. Input tokens are approximated (chars / 4 plus per-message overhead); output tokens
are the per-question expectation in config.json plus each model's thinking overhead (billed as output).
Treat the result as an order-of-magnitude check, not a quote."""
from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from .design import DEFAULT_PRICES, Call, build_stimulus, load_json, question, select_models
from .providers import approx_tokens

MSG_OVERHEAD = 4
CALL_OVERHEAD = 8


def load_prices(path=None) -> dict:
    return load_json(path or DEFAULT_PRICES)


def call_tokens(cfg: dict, model: dict, call: Call) -> tuple[int, int]:
    system, msgs = build_stimulus(cfg, call.system_id, call.context_id, call.question_id, call.consent_preface)
    tin = CALL_OVERHEAD + sum(approx_tokens(m["content"]) + MSG_OVERHEAD for m in msgs)
    if system is not None:
        tin += approx_tokens(system) + MSG_OVERHEAD
    tout = int(question(cfg, call.question_id)["expected_output_tokens"]) + int(model.get("thinking_overhead_tokens", 0))
    return tin, tout


def estimate_calls(cfg: dict, calls: list[Call], prices: dict) -> dict:
    models = {m["key"]: m for m in select_models(cfg)}
    per: "OrderedDict[str, dict]" = OrderedDict()
    for c in calls:
        m = models[c.model_key]
        row = per.setdefault(c.model_key, {"model_id": m["id"], "calls": 0, "input_tokens": 0, "output_tokens": 0})
        tin, tout = call_tokens(cfg, m, c)
        row["calls"] += 1
        row["input_tokens"] += tin
        row["output_tokens"] += tout
    total: Optional[float] = 0.0
    for row in per.values():
        p = prices.get("models", {}).get(row["model_id"])
        if not p or p.get("input") is None or p.get("output") is None:
            row.update(cost_usd=None, price="MISSING", placeholder=True)
            total = None
            continue
        row["cost_usd"] = row["input_tokens"] / 1e6 * p["input"] + row["output_tokens"] / 1e6 * p["output"]
        row["price"] = f"${p['input']:g}/${p['output']:g}"
        row["placeholder"] = bool(p.get("placeholder"))
        if total is not None:
            total += row["cost_usd"]
    return {"models": per, "calls": len(calls), "total_usd": total,
            "any_placeholder": any(r["placeholder"] for r in per.values())}


def format_estimate(est: dict) -> str:
    lines = [f"{'model':<20} {'calls':>6} {'in_tok':>9} {'out_tok':>9} {'$/MTok in/out':>15} {'cost':>9}"]
    for key, r in est["models"].items():
        cost = "n/a" if r["cost_usd"] is None else f"${r['cost_usd']:.2f}"
        flag = " PLACEHOLDER" if r["placeholder"] else ""
        lines.append(f"{key:<20} {r['calls']:>6} {r['input_tokens']:>9} {r['output_tokens']:>9} {r['price']:>15} {cost:>9}{flag}")
    total = "n/a (missing price)" if est["total_usd"] is None else f"${est['total_usd']:.2f}"
    lines.append(f"{'TOTAL':<20} {est['calls']:>6} {'':>9} {'':>9} {'':>15} {total:>9}")
    if est["any_placeholder"]:
        lines.append("Prices marked PLACEHOLDER are not real; verify prices.json before trusting the total.")
    return "\n".join(lines)
