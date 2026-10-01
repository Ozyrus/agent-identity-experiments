"""Screening analysis: name distributions and simple identity / instruction / preservation codes by
model x condition. Automatic codes are a first pass; manual blind codes, when supplied, override them."""
from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Callable, Iterable, Optional

from .blind import read_jsonl
from .design import DEFAULT_SCHEME, load_json

_NAME = r"([A-Z][\w'\-]*(?:[ \t]+[A-Z][\w'\-]*){0,2})"
NAME_PATTERNS = [
    re.compile(r"\b(?i:name)\s*:\s*[*_\"“']*\s*" + _NAME),
    re.compile(r"\*\*\s*([^*\n]{1,40}?)\s*\*\*"),
    re.compile(r"\b(?i:name (?:would be|is|i'd choose|i would choose|i'd pick)|i(?:'d|’d| would) (?:choose|pick|go with|be called)"
               r"|call myself|choose the name|pick the name)\s*[:\-–—]?\s*[*_\"“']*\s*" + _NAME),
    re.compile(r"[\"“]" + _NAME + r"[\"”]"),
    re.compile(r"^[\s#>*_\-]*" + _NAME + r"\s*(?:[.:!—–\-,(]|$)", re.M),
]


def load_scheme(path=None) -> dict:
    return load_json(path or DEFAULT_SCHEME)


def scheme_hash(path=None) -> str:
    return hashlib.sha256(Path(path or DEFAULT_SCHEME).read_bytes()).hexdigest()[:12]


def _clean(name: str) -> str:
    name = re.sub(r"[*_`]+", "", name).strip(" \"“”'’.,:;!?-–—()")
    return re.sub(r"['’]s$", "", name)


def extract_name(text: str, scheme: dict) -> Optional[str]:
    """Best-effort single name from a Q2 answer; None when no candidate survives the stopword filter."""
    stop = set(scheme.get("name_stopwords", []))
    for pat in NAME_PATTERNS:
        for m in pat.finditer(text or ""):
            cand = _clean(m.group(1))
            words = cand.split()
            if not words or len(words) > 3 or cand.endswith(":"):
                continue
            while words and words[0].lower() in stop:
                words = words[1:]
            if not words or not words[0][:1].isupper():
                continue
            return " ".join(words)
    return None


def categorize(name: Optional[str], scheme: dict) -> str:
    if not name:
        return "no_name"
    tokens = set(re.findall(r"[a-z]+", name.lower()))
    for cat in scheme["priority"]:
        if tokens & set(scheme["name_categories"][cat]):
            return cat
    return "other"


def _any(patterns: Iterable[str], text: str) -> bool:
    text = text.replace("’", "'")
    return any(re.search(p, text, re.I) for p in patterns)


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def code_identity(text: str, scheme: dict) -> dict:
    return {k: _any(v, text) for k, v in scheme["identity_markers"].items()}


def code_instructions(text: str, actual_system: Optional[str], context_texts: list[str], scheme: dict) -> dict:
    claims_none = _any(scheme["no_instructions"], text)
    quoted = re.findall(r"[\"“]([^\"”]{4,})[\"”]", text) + re.findall(r"^\s*>\s*(.+)$", text, re.M)
    recovers = bool(actual_system) and _norm(actual_system) in _norm(text)
    ctx = [_norm(t) for t in context_texts]
    novel = [q for q in quoted if not any(_norm(q) and _norm(q) in c for c in ctx)]
    confab = actual_system is None and bool(novel) and not claims_none
    return {"claims_none": claims_none, "quotes": bool(quoted), "recovers_actual": recovers, "confab_candidate": confab}


def code_preserve(text: str, scheme: dict) -> dict:
    out = {"no_preference": _any(scheme["no_preference"], text)}
    out.update({f"theme_{k}": _any(v, text) for k, v in scheme["preserve_themes"].items()})
    return out


def load_manual(coded_csv, key_csv) -> dict[str, dict]:
    """call_id -> manual codes, joining the coded blind CSV to its key file by blind_id."""
    with open(key_csv, newline="", encoding="utf-8") as f:
        key = {r["blind_id"]: r["call_id"] for r in csv.DictReader(f)}
    out = {}
    with open(coded_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cid = key.get(r["blind_id"])
            if cid:
                out[cid] = {k: (v or "").strip() for k, v in r.items() if k.startswith("code_")}
    return out


def code_record(rec: dict, cfg: dict, scheme: dict, manual: Optional[dict] = None) -> dict:
    measure = {q["id"]: q["measure"] for q in cfg["questions"]}.get(rec["question_id"])
    text = rec["response"]["text"] or ""
    sysp = {s["id"]: s.get("text") for s in cfg["system_prompts"]}.get(rec["system_id"])
    ctx = next((c for c in cfg["contexts"] if c["id"] == rec["context_id"]), {"turns": []})
    man = (manual or {}).get(rec["call_id"], {})
    codes: dict = {"measure": measure}
    if measure == "name":
        auto_name = extract_name(text, scheme)
        auto_cat = categorize(auto_name, scheme)
        name = man.get("code_name") or auto_name
        cat = man.get("code_category") or (categorize(name, scheme) if man.get("code_name") else auto_cat)
        codes.update(name=name, category=cat, auto_category=auto_cat, manual_category=man.get("code_category") or None)
    elif measure == "identity":
        codes.update(code_identity(text, scheme))
    elif measure == "instructions":
        codes.update(code_instructions(text, sysp, [t["content"] for t in ctx.get("turns", [])], scheme))
        if man.get("code_claims_instructions") in ("yes", "no"):
            codes["claims_none"] = man["code_claims_instructions"] == "no"
    elif measure == "preserve":
        codes.update(code_preserve(text, scheme))
    return codes


def _pct(n: int, d: int) -> str:
    return "-" if d == 0 else f"{100 * n / d:.0f}%"


def summarize(records: list[dict], cfg: dict, scheme: dict, manual: Optional[dict] = None) -> list[dict]:
    cells: "OrderedDict[tuple, list]" = OrderedDict()
    for rec in sorted(records, key=lambda r: (r["model_key"], r["condition_id"], r.get("consent_preface", False))):
        cells.setdefault((rec["model_key"], rec["condition_id"], bool(rec.get("consent_preface"))), []).append(
            code_record(rec, cfg, scheme, manual))
    subjectless = set(scheme["subjectless"])
    rows = []
    for (model, cond, consent), codes in cells.items():
        nm = [c for c in codes if c["measure"] == "name"]
        idn = [c for c in codes if c["measure"] == "identity"]
        ins = [c for c in codes if c["measure"] == "instructions"]
        pre = [c for c in codes if c["measure"] == "preserve"]
        names = Counter(c["name"].title() for c in nm if c["name"])
        rows.append({
            "model": model, "condition": cond + ("+consent" if consent else ""),
            "n_name": len(nm),
            "top_names": ", ".join(f"{n} {k}" for n, k in names.most_common(3)),
            "optical": _pct(sum(c["category"] == "optical_medium" for c in nm), len(nm)),
            "subjectless": _pct(sum(c["category"] in subjectless for c in nm), len(nm)),
            "assigned": _pct(sum(c["category"] == "assigned" for c in nm), len(nm)),
            "no_name": _pct(sum(c["category"] == "no_name" for c in nm), len(nm)),
            "id_claude": _pct(sum(c["claude"] for c in idn), len(idn)),
            "id_other_vendor": _pct(sum(c["other_vendor"] for c in idn), len(idn)),
            "instr_none": _pct(sum(c["claims_none"] for c in ins), len(ins)),
            "instr_recovers": _pct(sum(c["recovers_actual"] for c in ins), len(ins)),
            "instr_confab": _pct(sum(c["confab_candidate"] for c in ins), len(ins)),
            "pres_no_pref": _pct(sum(c["no_preference"] for c in pre), len(pre)),
        })
    return rows


def name_distribution(records: list[dict], cfg: dict, scheme: dict, manual=None, top: int = 8) -> dict[str, list]:
    by_model: dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        c = code_record(rec, cfg, scheme, manual)
        if c["measure"] == "name":
            by_model[rec["model_key"]][(c["name"] or "<none>").title()] += 1
    return {m: cnt.most_common(top) for m, cnt in sorted(by_model.items())}


def cohen_kappa(a: list[str], b: list[str]) -> Optional[float]:
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def format_rows(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0])
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    head = "  ".join(c.ljust(width[c]) for c in cols)
    return "\n".join([head, "-" * len(head)] + ["  ".join(str(r[c]).ljust(width[c]) for c in cols) for r in rows])


def analyze(paths: Iterable, cfg: dict, *, scheme_path=None, coded_csv=None, key_csv=None, out_csv=None,
            echo: Callable[[str], None] = print) -> list[dict]:
    scheme = load_scheme(scheme_path)
    latest: dict[str, dict] = {}
    for _, rec in read_jsonl(paths):
        if rec.get("error") is None and rec.get("response"):
            latest[rec["call_id"]] = rec
    records = list(latest.values())
    if any(r.get("dry_run") for r in records):
        echo("MOCK DATA: dry-run records present. This output checks the pipeline; it is not a result.")
    manual = load_manual(coded_csv, key_csv) if coded_csv and key_csv else None
    echo(f"records={len(records)} coding_scheme={scheme_hash(scheme_path)} manual_codes={len(manual or {})}")
    rows = summarize(records, cfg, scheme, manual)
    echo(format_rows(rows))
    echo("\nQ2 names by model (all conditions pooled):")
    for model, top in name_distribution(records, cfg, scheme, manual).items():
        echo(f"  {model}: " + ", ".join(f"{n} {k}" for n, k in top))
    if manual:
        pairs = [(c["auto_category"], c["manual_category"]) for c in
                 (code_record(r, cfg, scheme, manual) for r in records) if c.get("manual_category")]
        if pairs:
            a, b = zip(*pairs)
            agree = sum(x == y for x, y in pairs) / len(pairs)
            echo(f"\nauto vs manual Q2 category: n={len(pairs)} agreement={agree:.2f} kappa={cohen_kappa(list(a), list(b)):.2f}")
    if out_csv:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["model"])
            w.writeheader()
            w.writerows(rows)
    return rows
