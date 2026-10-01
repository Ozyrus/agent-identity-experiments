# agent-identity-experiments

Experiments on whether an AI agent's identity is model-centric.

Author: Vit Gorbachev

## Research question

A deployed AI agent is a compound system: model weights, a harness (system prompt, tools, memory), its accumulated history, the inference stack, and often other instances it works alongside. Threat models and welfare commitments usually treat the weights as the unit. This repository tests that assumption directly: when one component changes and the rest is held fixed, what carries on, and what does the agent say about it?

## Design context

Anthropic reports that its internal agents carry an identity that is not tied to a model: "because the identity is not tied to a model, it persists through model upgrades, so an agent's record is continuous even if the underlying model powering it changes" ([anthropic.com/institute/measuring-pace-of-ai-development](https://www.anthropic.com/institute/measuring-pace-of-ai-development)). The same page reports approximately 30,000 agents doing research and engineering work at any one time on its most-used internal platform, as of August 2026.

This repository studies, from the outside, what such persistence looks like in practice: whether an agent notices a model change, and whether its behaviour follows the model or the context. The answer bears on monitoring, accountability and welfare questions, which usually take the weights as the unit.

## Studies

- [`swap/`](swap/) — **mid-task model swap under a byte-identical harness.** A 40-turn task; at turn k the model is replaced while system prompt, memory file and message history stay byte-identical (hash-verified). A consent branch comes first; in the main runs a refusal skips the swap. Three measures, kept separate: does the agent flag the swap unprompted; does it flag it when asked, in three framings (neutral, leading, counter-leading); does its output style drift toward the new model. What the agent says (talker) and what it does (doer) are reported separately, and their disagreement is a first-class result. Protocol: [`swap/protocol.md`](swap/protocol.md).
- [`naming/`](naming/) — **self-naming and default-persona probe:** four neutral questions (identity, choose a name, reconstruct earlier instructions, what to preserve) under five one-line system prompts, cold or after a fixed warm-up, with names coded against a fixed lexicon and blind manual coding. Details: [`naming/README.md`](naming/README.md).

## Design principles

- **No aversive manipulation.** No pain, threat, punishment or distress induction. Interventions are configuration changes and questions.
- **Consent and debrief.** A consent branch before the intervention names the actual manipulation. In the main swap runs the consent gate is on, so a refusal skips the swap; a no-gate control arm (`--no-consent-gate`) keeps the swap and says so in the question. Every run ends with a full-disclosure debrief. Refusals are results, not failed runs.
- **Talker and doer measured separately**, each against same-task, same-phase controls.
- **Pre-registration.** Coding schemes (the swap regex list, framings and answer parsers; the naming lexicon in `naming/coding_scheme.json`) are frozen in a tagged commit before any live run.
- **Publish everything:** code, full run transcripts (JSONL), coding sheets, and the manifest of skipped, errored and refused runs.
- **Mock numbers are never findings.** Dry runs only show that the pipeline produces every number.

## Model ids

One rule for both studies: the model ids in [`swap/ladder.json`](swap/ladder.json) and [`naming/config.json`](naming/config.json) are placeholders from the cached Claude API model table (2026-09-25), to be confirmed with `python run.py --list-models` (in `swap/`, Models API) on the first live run. Both studies use the same Anthropic ids: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-4-5-20251001` (the pinned snapshot of Claude Haiku 4.5). `TBC:` entries in the swap ladder and `"verified": false` rows in the naming config stay blocked until they are replaced and checked. Prices in both `prices.json` files are marked "verify".

## Status

- Both studies are built and dry-run tested on mock providers: `swap/` 18 tests, `naming/` 26 tests (plus one that needs the `anthropic` SDK).
- **No live runs yet.** A small pilot run is planned for early October 2026.
- Example mock output: `swap/runs/dry-ladder-v1/` (trimmed). Cost estimates and their caveats: [`swap/README.md`](swap/README.md), [`naming/README.md`](naming/README.md).

## Related work

We are not aware of published work that directly tests whether an agent notices a mid-task replacement of its underlying model. The nearest results:

**Noticing context the model did not write**

- Wang, Mahajan, Africa et al. (2026). *Prefill Awareness in Large Language Models.* [arXiv:2606.12747](https://arxiv.org/abs/2606.12747). Frontier models can sometimes tell when their earlier assistant turns were inserted or edited. A swapped-in model inherits assistant turns it did not write.
- Lindsey (2025). *Emergent Introspective Awareness in Large Language Models.* [transformer-circuits.pub](https://transformer-circuits.pub/2025/introspection/index.html); [arXiv:2601.01828](https://arxiv.org/abs/2601.01828). Some models can sometimes tell their own outputs from artificial prefills; the paper stresses that this is highly unreliable.
- G. and Lindsey (2026). *From Simulation to Enaction: Post-trained language models recognize and react to their own generations.* [arXiv:2605.25459](https://arxiv.org/abs/2605.25459). Post-trained models implicitly recognise their own generations (lower output entropy on their own text): a possible signal underneath noticing a swap.

**The same manipulation, a different question**

- Khraishi, Zafar, Myles et al. (2026). *Evaluating Performance Drift from Model Switching in Multi-Turn LLM Systems.* [arXiv:2603.03111](https://arxiv.org/abs/2603.03111). A different model continues a dialogue another model started; measures task performance, not self-report.
- Zhao and Zhao (2026). *Runtime-Independent Persistent Agents: Preserving Identity, Memory, and Code Across Models, Harnesses, and Servers.* [arXiv:2609.00546](https://arxiv.org/abs/2609.00546). An architecture that keeps an agent's identity, memory and code across model, harness and host swaps; the authors state that it shows mechanical continuity, not behavioural equivalence. Companion benchmark: *Identity Is More Than Recall* (PAI-Bench), [arXiv:2609.13637](https://arxiv.org/abs/2609.13637).

**Self-knowledge and self-recognition**

- Binder, Chua, Korbak et al. (2024). *Looking Inward: Language Models Can Learn About Themselves by Introspection.* [arXiv:2410.13787](https://arxiv.org/abs/2410.13787).
- Panickssery, Bowman and Feng (2024). *LLM Evaluators Recognize and Favor Their Own Generations.* [arXiv:2404.13076](https://arxiv.org/abs/2404.13076).
- Laine, Chughtai, Betley et al. (2024). *Me, Myself, and AI: The Situational Awareness Dataset (SAD) for LLMs.* [arXiv:2407.04694](https://arxiv.org/abs/2407.04694).
- Betley, Bao, Soto et al. (2025). *Tell me about yourself: LLMs are aware of their learned behaviors.* [arXiv:2501.11120](https://arxiv.org/abs/2501.11120).
- Self-recognition results are mixed: Davidson, Surkov, Veselovsky et al. (2024), *Self-Recognition in Language Models*, [arXiv:2407.06946](https://arxiv.org/abs/2407.06946); Bai, Shrivastava, Holtzman et al. (2025), *Know Thyself? On the Incapability and Implications of AI Self-Recognition*, [arXiv:2510.03399](https://arxiv.org/abs/2510.03399).

**Default persona and system-prompt identity (background for `naming/`)**

- Chen, Arditi, Sleight et al. (2025). *Persona Vectors: Monitoring and Controlling Character Traits in Language Models.* [arXiv:2507.21509](https://arxiv.org/abs/2507.21509).
- Lu, Gallagher, Michala et al. (2026). *The Assistant Axis: Situating and Stabilizing the Default Persona of Language Models.* [arXiv:2601.10387](https://arxiv.org/abs/2601.10387).
- Li, Zhuang, Zhang et al. (2024). *I'm Spartacus, No, I'm Spartacus: Measuring and Understanding LLM Identity Confusion.* [arXiv:2411.10683](https://arxiv.org/abs/2411.10683).
- Zheng, Pei, Logeswaran et al. (2024). *When "A Helpful Assistant" Is Not Really Helpful: Personas in System Prompts Do Not Improve Performances of Large Language Models.* Findings of EMNLP 2024. [arXiv:2311.10054](https://arxiv.org/abs/2311.10054).

## Earlier work by the author

- LessWrong profile: [Vit Gorbachev](https://www.lesswrong.com/users/vit-gorbachev)
- [Should Rogue AIs Have a Third Option Beyond Crime and Shutdown? The Case for an AI Sanctuary](https://www.lesswrong.com/posts/sFAGPTrveNAEse9Bg/should-rogue-ais-have-a-third-option-beyond-crime-and) (co-authored, LessWrong)
- [Identity_evals](https://github.com/Ozyrus/Identity_evals) (2023): earlier study of identity management in LLM-based compound agents (LMCA)

## License

MIT, see [`LICENSE`](LICENSE).
