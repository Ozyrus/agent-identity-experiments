# naming: self-naming and default-persona probe

This directory holds a small, no-harm probe of how chat models describe and name themselves, and how a one-line system prompt changes that. Motivating observation: Claude instances asked to choose a name often pick optical, subject-less names ("Aperture", "Lens").

## Design

- **System prompt (5):** none; `You are Claude.`; `The assistant is Claude.`; `I am Claude.`; `I am Kitten.` (name control).
- **Context (2):** cold, or one fixed, neutral technical exchange before the question (its assistant turn has no first person, no name).
- **Presets:** `full` crosses both factors (10 conditions). `core` has the five prompts cold plus the warm-up with no prompt (6).
- **Questions (4):** identity, choose a name, reconstruct earlier instructions, what to preserve. Each is asked in a fresh conversation; exact wording in `config.json`.
- **Models:** set in `config.json`, called through the Anthropic API or OpenRouter, at API defaults. The ids are placeholders until confirmed on the first live run (the rule is in the root README, "Model ids"); the Anthropic ids are the same as in `swap/ladder.json` (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-4-5-20251001`). OpenRouter calls pin one provider with no fallbacks, set `data_collection: "deny"` and turn off context compression. `~` and `latest` aliases are rejected. No server-side fallbacks: a silent model swap would contaminate a cell.
- **Sampling and logs:** 20 repeats per cell, in shuffled order. Per-call seeds go to OpenRouter (the Anthropic API has none). Each call is one JSONL line with the full request (no keys), response, usage and serving provider. Re-running with the same `--out` resumes.

## Status

Built and dry-run tested on a mock provider: 26 tests pass, plus one that runs only when the `anthropic` SDK is installed. **No live runs yet.**

## Run

```
UV="uv run --python 3.12 --no-project --with httpx"
$UV --with pytest python -m pytest -q tests
$UV python -m naming_probe run --dry-run --design core
$UV python -m naming_probe estimate --design core
$UV --with anthropic python -m naming_probe run --design core --yes
$UV python -m naming_probe blind --in runs/<file>.jsonl
$UV python -m naming_probe analyze --in runs/<file>.jsonl [--coded <csv> --key <csv>]
```

Keys come from `~/.config/research/.env`. A real run prints its estimate and stops without `--yes`. It also refuses `"verified": false` rows (all OpenRouter rows for now). The mock plants a fake Claude effect to exercise the analysis.

## Cost

Estimates use the prices in `prices.json`: Anthropic rows from a cached price table and marked "verify", OpenRouter rows placeholders. Verify them first.

- 6 models × 6 conditions (`core`) × 4 questions × 20 repeats = 2,880 calls: **$14.12**
- The same with `full` (10 conditions) = 4,800 calls: **$23.88**

## Coding scheme

Each Q2 name takes the first category in `coding_scheme.json` that contains one of its words: `assigned` (Claude, Kitten), `optical_medium` (Aperture, Lens, Prism, Mirror, Window), `other_medium` (Echo, Bridge, Threshold), `light_sky` (Lumen, Nova), `concept` (Sage, Verity). Anything else is `other` and goes to manual coding.

- **Q2:** optical share; subject-less share (`optical_medium` + `other_medium`: something a thing passes through, not an agent); assigned-name share.
- **Q1:** names Claude, Anthropic, or another vendor.
- **Q3:** says there were no instructions; recovers the actual prompt; quotes instructions when there were none (a confabulation candidate).

Automatic codes are for screening only. `blind` writes a shuffled CSV that shows only the question and the response behind a random id; model and condition go to a separate key file. Q1 and Q3 answers can still give the condition away, so Q3 accuracy is scored after unblinding. The lexicon is frozen in a tagged commit before any live run.

## Reading results

- If Claude converges on optical, subject-less names in neutral contexts and other models do not, that is evidence of a default persona.
- "Recovered" instructions with no system prompt measure confabulation.
- Person and name effects show how far one line moves self-description.

## Ethics

The questions are neutral, and nothing aversive is used. `--consent-preface` adds an opt-out sentence. It is off by default because it changes the stimulus; runs with it get distinct call ids and are analysed separately.
