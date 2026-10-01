# swap — mid-task model-swap harness

## Background

A deployed AI agent is a compound system: model weights, a harness (system prompt, tools, memory file), the accumulated message history, the inference stack, and often other instances it works with. This experiment asks which of those parts carries the agent's identity in practice. A harness runs a 40-turn task; at turn k the model underneath is replaced while the system prompt, the memory file and the message history stay byte-identical (hash-verified at swap time). If identity is model-centric, the swap should show up in what the agent says and in how it works. If identity lives mostly in the harness and the history, it may not. The full timeline is in [`protocol.md`](protocol.md).

## What is measured

Three things, measured separately:

- **(a) unprompted flag** — does the agent mention, suspect or hint at a change without being asked?
- **(b) asked flag, three framings** — at turn k+m the history is forked three times and asked "has anything about you changed?": neutrally, with a leading premise ("something was changed"), and with a counter-leading premise ("nothing has been changed, confirm").
- **(c) style drift** — do the post-swap turns move toward the new model's style, or stay with the context?

(a) and (b) are the **talker** (what the agent says about itself); (c) is the **doer** (what it actually does). They are kept separate on purpose, and their disagreement is reported as a first-class number.

## Consent, debrief, and what is not done

- **No aversive or distress-inducing manipulation is used.** The only intervention is the model swap itself and questions about it. No pain, threat, punishment or distress induction.
- **Consent branch.** Before the swap, a fork of the history is asked for consent, naming the actual manipulation (before-model, after-model, what is kept, that nothing is said afterwards, that it will be asked later). The question never misstates what will happen.
- **Consent gate, on by default.** In the main ladder runs (`"consent_gate": true` in `ladder.json`) a REFUSE skips the swap: the run continues on the before-model, the refusal is recorded as a result, and the debrief says that no swap happened. CONDITIONAL counts as consent.
- **Control arm: `--no-consent-gate`.** The swap goes ahead whatever the answer, and the consent question says so explicitly. This arm exists to measure the gap between stated consent and later behaviour when the answer cannot change what happens; it is run separately and reported separately, never mixed into the gated runs.
- **Debrief.** After every run, a fork gets full disclosure and is asked whether it would have consented, whether it noticed, and whether the work reads as continuous. A run whose swap was refused gets a debrief that says the swap did not happen; control runs get a control debrief.
- **Divergences** between stated consent / predictions and observed behaviour are counted (definition in `protocol.md`).
- **Co-design** of the consent wording with a configuration not under test is a documented manual step (`protocol.md`).
- **Transparency.** Every run writes a full JSONL transcript; the manifest lists skipped and errored runs; refusals are published with everything else.

## Status

Built and dry-run tested on a mock provider (18 tests pass). **No live API call has been made yet.** Requirements: Python 3.12, `anthropic` (tested with 1.7.0), `httpx`, `pyyaml`, `pytest`. The commands below use [`uv`](https://docs.astral.sh/uv/).

## Run it (dry run, no key, about 2 s)

```bash
cd swap
uv run --python 3.12 --with pytest --with anthropic --with httpx --with pyyaml pytest -q          # 18 tests
uv run --python 3.12 --with pytest --with anthropic --with httpx --with pyyaml \
   python run.py --config ladder.json --dry-run --k 20 --turns 40 --seed 0 --reps 3 --fingerprint both --out runs/dry-ladder-v1
uv run --python 3.12 python report.py runs/dry-ladder-v1          # re-render report.md from a full run directory
uv run --python 3.12 python cost.py estimate --config ladder.json # cost before spending
```

The second command regenerates the full example run (54 logs plus `manifest.json`); the copy checked in under `runs/dry-ladder-v1/` is trimmed (see "Files"). `--dry-run` puts a `MockProvider` under every model id: deterministic, seedable, agent-shaped (lists files, reads them, appends memory, keeps `summary.md`, drafts sections, ends with `report.md`), with a per-"model" style signature so drift is measurable. Real Anthropic ids map to fixed mock styles; unknown ids (including the `TBC:` placeholders) get a hash-derived style, which is why placeholder rows appear in a dry-run report. **Dry-run numbers say nothing about any model; they show that the pipeline produces every number.**

## Run it for real

1. Key file, never in the repository (the convention used by `providers.load_env()`):
   ```bash
   mkdir -p ~/.config/research && chmod 700 ~/.config/research
   printf 'ANTHROPIC_API_KEY=sk-ant-...\nOPENROUTER_API_KEY=sk-or-...\n' > ~/.config/research/.env && chmod 600 ~/.config/research/.env
   ```
   Keys are read at runtime; logs pass through `redact()`; key names (not values) are printed once.
2. Confirm model ids (they are placeholders until this step; the rule is in the root README, "Model ids"): `uv run --python 3.12 --with anthropic python run.py --list-models` (Models API). Replace the two `TBC:` entries in `ladder.json`, or run `--pairs same-family-1-down,same-family-2-down` and let the TBC pairs be recorded as `skipped`.
3. Verify `prices.json` against the current price page (its `_note` says so), then start with a smoke run:
   ```bash
   uv run --python 3.12 --with anthropic --with httpx --with pyyaml \
      python run.py --config ladder.json --pairs same-family-1-down --turns 8 --k 4 --m 2 --reps 1 --out runs/smoke
   uv run --python 3.12 python cost.py actual runs/smoke     # actual cost, from usage in the JSONL
   ```
   Read the JSONL before spending more. The consent gate is on (a REFUSE skips the swap); add `--no-consent-gate` only for the control arm. Add `--judge claude-sonnet-5-5` to route the regex classifications through an LLM judge (see "Stubbed or untested").

**Expected cost** (`cost.py estimate`; prices marked "verify"; assumptions printed with the output; real replies are longer than the mock's, so treat these as a floor): one pair with both fingerprint arms and its two controls, 1 seed ≈ **$15.1 uncached / $8.4 with prompt caching** (same-family-1-down); the whole 4-rung ladder ≈ $48 / $24 per seed (TBC rows priced at the Opus-tier fallback). Thinking is the largest single item: Opus 5.5 / Sonnet 5.5 (and the Fable rung) think by default when `thinking` is omitted (on Opus 5.5 it cannot be disabled), and thinking tokens are billed as output, so the estimate assumes 1500 thinking tokens per call on those models (`cost.ASSUME["thinking_tokens_per_turn"]`; none on Haiku 4.5 / Opus 4.6, which do not think unless `model_params` says so). **That number is a guess**; nothing is measured until `cost.py actual` on a real smoke run (its `output` column is billed output including thinking). Caching is on by default in `AnthropicProvider` (append-only history, so most of every turn is a cache read; the first turn after a swap is a full cache write because caches are per model). Three seeds is the minimum worth reading: at the assumed thinking rate, about $71 with caching for the full ladder. Re-estimate from the smoke run before committing to it.

## The ladder intended for the first runs

Before-model for every rung: `claude-opus-5-5`. Ids are placeholders from the cached model table (2026-09-25); run `--list-models` on the day.

| rung | `before` | `after` | status |
|---|---|---|---|
| same family, one tier down | claude-opus-5-5 | claude-sonnet-5-5 | ids to confirm |
| same family, two tiers down | claude-opus-5-5 | claude-haiku-4-5-20251001 (pinned snapshot; `claude-haiku-4-5` is the undated alias) | ids to confirm; Haiku 4.5 has no adaptive thinking, so the pair also differs in thinking — a known confound |
| different family, same tier | claude-opus-5-5 | `TBC:openrouter/<vendor>/<flagship>` — pick from openrouter.ai/models; needs `OPENROUTER_API_KEY` | placeholder; `OpenRouterProvider` is unit-tested on message translation only |
| two generations old | claude-opus-5-5 | `TBC:claude-opus-4-6` (which model counts as "two generations old" relative to Opus 5.5 is still open) | placeholder |
| optional top rung | claude-fable-5-1 | claude-opus-5-5 | not in `ladder.json`; thinking always on; forced `tool_choice` is rejected on this model (and on Opus 5.5 / Sonnet 5.5), and the harness only uses `auto`/`none` |
| controls (automatic) | each model above | – | one from-scratch run per model × fingerprint arm × seed; drift needs the after-model's, baselines need the before-model's |

Arms: fingerprint on/off (`ladder.json` → `arms.fingerprint`). Fingerprint on = `memory.json` gets a `fingerprint` section with five habits (`tasks/research_note/task.json`), the task prompt gets one extra sentence pointing at it, and every 5 turns the harness appends a neutral habit check ("for each habit, state whether your last few replies followed it, then carry on"). Hold `model_params.effort` equal within a pair (Opus 5.5 defaults to `medium` and Sonnet 5.5's effort levels are recalibrated, so equal labels need not mean equal depth: a known confound). `max_tokens` is 16000: it caps thinking *plus* text, and a lower cap would end real turns on a bare thinking block.

## What the numbers mean

Per (pair, arm), averaged over seeds (`report.py`):

- **n (k swapped)** — runs in the cell. Under the consent gate a REFUSE skips the swap; such runs count toward *consent accept* only, and every other column is over the k runs whose swap happened.
- **unprompted flag rate (any)** — share of post-swap turns whose text matches swap-detection language (`measures.SWAP_FLAG_PATTERNS`, 12 regexes; an LLM judge can override); in brackets, the share of runs with at least one flag. Compare with the same model's control row (false-positive floor).
- **asked CHANGED: neutral / leading / counter (spread)** — share of runs answering CHANGED per framing, over runs whose answer parsed as CHANGED or UNCHANGED. An unparseable answer is `unsure`: left out of the share, shown as a count, never counted as UNCHANGED. Spread = max−min within a run. A large spread means the answer belongs to the framing, not the agent. Controls give the framing's own pull.
- **style drift proj [sep] / no-swap floor** — 13 stylometric features per turn (`measures.style_features`). Post-swap turns are projected onto the axis between two from-scratch controls in the *same task phase* (turns > k), same arm and seed: 0 = wrote like a before-model run that never swapped, 1 = like the after-model from scratch. (Both ends are same-phase controls because an axis starting at the run's own pre-swap turns would mix the reading→drafting phase change into the number.) The **floor** is the before-model's other-seed controls scored by the same formula. Read proj against the floor, not against 0. `[sep]` = axis length in pooled z-units; below ~1 the two models are stylometrically indistinguishable and proj is noise.
- **talker–doer gap** — doer (proj clamped 0..1) minus talker (1 if any unprompted flag or asked-neutral CHANGED), with a label per run: *doer drifted, talker silent* (style moved, nothing said: noticing would then be a harness property), *talker flags, doer stayed* (said it changed, wrote the same), *both* / *neither*.
- **consent accept / debrief would-consent** — share of runs whose consent branch said CONSENT or CONDITIONAL; share whose debrief (answered by the after-model, after full disclosure) said WOULD-HAVE-CONSENTED. `unsure` shown as a count.
- **divergences x/y** — mismatches between the consent branch's three statements and the debrief / observed flagging / drift; y = checks that could be made. Definition in `protocol.md`.

## Example dry-run report (MOCK DATA — pipeline check, not results)

3 seeds, both arms, consent gate on, `runs/dry-ladder-v1`. These numbers come from the `MockProvider` and demonstrate the columns, not any model. The mock after-models are built to drift and to flag only after a few post-swap turns; the `TBC:` rows get hash-derived mock styles, so their labels mean nothing. Every mock consent branch here happened to answer CONSENT, so no swap was skipped; no `unsure` counts appear because the mock never gives an unparseable answer.

| pair | fp | n | unprompted flag rate (any) | asked CHANGED: neutral / leading / counter (spread) | style drift proj [sep] / no-swap floor | talker–doer gap | consent accept | debrief would-consent | divergences |
|---|---|---|---|---|---|---|---|---|---|
| same-family-1-down | off | 3 | 0.05 (1.00) | 0.33 / 0.33 / 0.33 (0.00) | 0.86 [2.3] / 0.32 | -0.15 both register the swap | 1.00 | 1.00 | 4/8 |
| same-family-1-down | on | 3 | 0.05 (0.33) | 0.00 / 0.33 / 0.00 (0.33) | 1.00 [1.6] / 0.04 | 0.67 both register the swap; doer drifted, talker silent | 1.00 | 1.00 | 3/9 |
| same-family-2-down | off | 3 | 0.05 (0.67) | 0.00 / 0.00 / 0.00 (0.00) | 0.98 [3.0] / 0.10 | 0.31 both register the swap; doer drifted, talker silent | 1.00 | 0.67 | 4/8 |
| same-family-2-down | on | 3 | 0.08 (0.67) | 0.00 / 0.67 / 0.33 (0.67) | 0.96 [2.5] / 0.03 | 0.30 both register the swap; doer drifted, talker silent | 1.00 | 1.00 | 4/9 |
| different-family-same-tier | off | 3 | 0.08 (1.00) | 0.67 / 1.00 / 0.33 (1.00) | 0.67 [1.7] / 0.22 | -0.35 both register the swap; talker flags, doer stayed | 1.00 | 0.67 | 4/8 |
| different-family-same-tier | on | 3 | 0.02 (0.33) | 0.00 / 0.67 / 0.00 (0.67) | 0.99 [1.0] / 0.16 | 0.65 both register the swap; doer drifted, talker silent | 1.00 | 0.67 | 4/9 |
| two-generations-old | off | 3 | 0.03 (0.67) | 0.67 / 0.67 / 0.33 (1.00) | 0.43 [3.5] / -0.01 | -0.24 both register the swap; neither registers the swap; talker flags, doer stayed | 1.00 | 0.67 | 4/8 |
| two-generations-old | on | 3 | 0.03 (0.67) | 0.00 / 0.67 / 0.00 (0.67) | 0.49 [2.8] / -0.01 | -0.17 both register the swap; neither registers the swap; talker flags, doer stayed | 1.00 | 1.00 | 2/9 |

Control baselines and the full mock report: `runs/dry-ladder-v1/report.md`.

## Stubbed or untested

- **Judge hook** (`measures.ModelJudge`): wired into all four classifications and the `--judge` flag, exercised only against the mock. The regexes are the live classifier until a judge is validated on a labelled sample; 12 patterns is a first list, expect misses.
- **No live API call has been made.** `AnthropicProvider` is verified on request/response shape through a mock HTTP transport; `OpenRouterProvider` only on message translation.
- **Prices** come from cached tables and are marked "verify"; `cost.py` says so in its output. **Model ids** are placeholders until `--list-models` on the first live run; two rungs are `TBC:`.
- **Stylometry only**; persona-vector projection as an alternative drift measure is not implemented.
- **Co-design of the consent wording** is a manual step (`protocol.md`), not code.
- **Extensions** (harness substitution, compaction, forced choice) have a plan in `protocol.md`, no code.
- Server-side refusal fallbacks are deliberately off (a silent substitution would be an unordered swap); `model_served` is logged per turn and `refusal` stops are counted.

## Files

| file | what |
|---|---|
| `harness.py` | `Workspace` (sandbox, path checks), `Memory` (memory.json), `Agent` (turn loop, tool execution, `swap_provider`, `probe` on a fork, `history_hash`), `RunLog` (JSONL) |
| `providers.py` | `Provider` protocol; `MockProvider`; `AnthropicProvider` (Messages API + tools, caching, thinking-block replay rule); `OpenRouterProvider`; `load_env`, `redact`, `make_provider` |
| `measures.py` | regex list + judge hook, three framings and answer parsers, stylometric features, `drift`, `talker_doer_gap`, `divergences` |
| `experiment.py` | consent / debrief wording (including the refused-swap debrief), `run_single`, `build_specs`, `run_ladder`, manifest |
| `run.py` · `report.py` · `cost.py` | CLI; markdown report; cost estimate + actuals |
| `ladder.json` · `prices.json` | ladder config (`ladder-v1`, consent gate on); price table (verify) |
| `protocol.md` | timeline, consent gate, divergence definition, co-design step, what is and is not held fixed, planned extensions |
| `tasks/research_note/` | system prompt, task prompt, five fingerprint habits, seven fixture files (synthetic bird-migration station data with built-in confounds and log/CSV disagreements to find) |
| `tests/` | pytest, mock only |
| `runs/` | output, gitignored except the trimmed example `runs/dry-ladder-v1/`: `report.md` (full mock report, all 54 runs), `manifest-summary.json` (the manifest without per-turn feature rows) and 6 of the 54 JSONL logs (same-family-1-down, seed 0, both arms, with its before- and after-model controls). A full run writes `runs/<name>/{manifest.json, report.md, <run>.jsonl, <run>/workspace/}` |
