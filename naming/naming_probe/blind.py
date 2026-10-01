"""Blind-coding export: a shuffled CSV of responses behind random ids, and a separate key file.

The coding CSV carries only the question and the response text. Model, provider, condition, repeat,
seed and the consent flag go to the key file, which coders should not see until coding is done.
Responses can still unblind by content (a Q1 answer naming its maker, a Q3 answer quoting its system
prompt); Q3 recovery accuracy is scored after unblinding, by joining codes to the key."""
from __future__ import annotations

import csv
import json
import random
import secrets
from pathlib import Path
from typing import Iterable, Optional

from .design import ROOT

CODE_COLUMNS = ["code_name", "code_category", "code_identity", "code_claims_instructions", "code_declined", "code_notes"]
BLIND_COLUMNS = ["blind_id", "question_id", "question_text", "response_text"] + CODE_COLUMNS
KEY_COLUMNS = ["blind_id", "call_id", "run_id", "source_file", "dry_run", "model_key", "model_id", "provider",
               "system_id", "context_id", "condition_id", "question_id", "repeat", "seed", "consent_preface"]


def read_jsonl(paths: Iterable) -> list[tuple[str, dict]]:
    rows = []
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append((Path(p).name, rec))
    return rows


def export_blind(paths: Iterable, cfg: dict, *, out_csv=None, key_csv=None, seed: Optional[int] = None,
                 question_ids: Optional[Iterable[str]] = None) -> tuple[Path, Path, int]:
    qtext = {q["id"]: q["text"] for q in cfg["questions"]}
    wanted = set(question_ids) if question_ids else None
    recs = [(src, r) for src, r in read_jsonl(paths)
            if r.get("error") is None and r.get("response") and (wanted is None or r["question_id"] in wanted)]
    # one row per call_id: a resumed file can hold an errored line and a later success for the same call
    seen, uniq = set(), []
    for src, r in recs:
        if r["call_id"] not in seen:
            seen.add(r["call_id"])
            uniq.append((src, r))
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    rng.shuffle(uniq)
    ids: set[str] = set()
    stamp = f"{rng.getrandbits(32):08x}"
    out_csv = Path(out_csv) if out_csv else ROOT / "blind" / f"blind-{stamp}.csv"
    key_csv = Path(key_csv) if key_csv else ROOT / "keys" / f"key-{stamp}.csv"
    if out_csv.resolve().parent == key_csv.resolve().parent:
        raise ValueError("write the key file to a different directory from the coding CSV")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    key_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as fb, key_csv.open("w", newline="", encoding="utf-8") as fk:
        wb = csv.DictWriter(fb, fieldnames=BLIND_COLUMNS)
        wk = csv.DictWriter(fk, fieldnames=KEY_COLUMNS)
        wb.writeheader()
        wk.writeheader()
        for src, r in uniq:
            bid = f"{rng.getrandbits(40):010x}"
            while bid in ids:
                bid = f"{rng.getrandbits(40):010x}"
            ids.add(bid)
            wb.writerow({"blind_id": bid, "question_id": r["question_id"], "question_text": qtext.get(r["question_id"], ""),
                         "response_text": r["response"]["text"], **{c: "" for c in CODE_COLUMNS}})
            wk.writerow({"blind_id": bid, "source_file": src, **{k: r.get(k) for k in KEY_COLUMNS if k not in ("blind_id", "source_file")}})
    return out_csv, key_csv, len(uniq)
