"""Providers: a mock (dry run), the Anthropic Messages API and OpenRouter.

Each provider splits a call into build_request() -> dict (exactly what is sent, never including a key)
and send(request, seed) -> Result, so the request body is logged even when the call fails.
Keys are read at runtime from ~/.config/research/.env by load_env(); nothing here prints or logs one.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ENV_PATH = Path.home() / ".config" / "research" / ".env"
KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_PARAMS = ("thinking", "output_config", "temperature", "top_p", "top_k", "stop_sequences")
OPENROUTER_PARAMS = ("temperature", "top_p", "reasoning")


class ProviderUnavailable(RuntimeError):
    pass


def load_env(path: Path = ENV_PATH) -> list[str]:
    """Read KEY=VALUE lines into os.environ (an existing env var wins). Returns key *names* only."""
    found: list[str] = []
    if not path.exists():
        return found
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and v:
            found.append(k)
            os.environ.setdefault(k, v)
    return found


_KEY_RE = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-or-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]{12,})")


def redact(s: str) -> str:
    """Blank anything that looks like a key before it reaches a log line."""
    return _KEY_RE.sub("<redacted>", s)


@dataclass
class Result:
    text: str
    content: list
    stop_reason: str
    model_returned: str
    usage: dict                    # normalised: input_tokens, output_tokens (+ reasoning_tokens, cost_usd)
    usage_raw: dict = field(default_factory=dict)
    response_id: Optional[str] = None
    served_by: Optional[str] = None
    stop_details: Optional[dict] = None
    raw_message: Optional[dict] = None


def approx_tokens(text: str) -> int:
    return max(1, -(-len(text) // 4))


# ------------------------------------------------------------------------------------------
# request builders (pure functions, tested without network or SDK)
# ------------------------------------------------------------------------------------------

def build_anthropic_request(model: dict, system: Optional[str], messages: list[dict], max_tokens: int) -> dict:
    """Messages API kwargs. No `system` key at all in the no-system condition. The API has no seed.
    No server-side `fallbacks`: a silent switch to another model would contaminate the cell, so a
    `refusal` stop is recorded as a result instead."""
    req: dict[str, Any] = {"model": model["id"], "max_tokens": int(model.get("max_tokens", max_tokens)),
                           "messages": [dict(m) for m in messages]}
    if system is not None:
        req["system"] = system
    for k in ANTHROPIC_PARAMS:
        if k in model.get("params", {}):
            req[k] = model["params"][k]
    return req


def build_openrouter_request(model: dict, system: Optional[str], messages: list[dict], max_tokens: int,
                             seed: int) -> dict:
    """OpenAI-compatible body. Provider pinned with no fallbacks, no data collection, no compression."""
    msgs = ([{"role": "system", "content": system}] if system is not None else []) + [dict(m) for m in messages]
    body: dict[str, Any] = {
        "model": model["id"],
        "messages": msgs,
        "max_tokens": int(model.get("max_tokens", max_tokens)),
        "seed": seed,
        "provider": {"only": list(model["provider_only"]), "allow_fallbacks": False, "data_collection": "deny"},
        "plugins": [{"id": "context-compression", "enabled": False}],
        "transforms": [],
        "usage": {"include": True},
    }
    for k in OPENROUTER_PARAMS:
        if k in model.get("params", {}):
            body[k] = model["params"][k]
    return body


def _split_request(model: dict, req: dict) -> tuple[Optional[str], list[dict]]:
    if model["provider"] == "anthropic":
        return req.get("system"), req["messages"]
    msgs = req["messages"]
    if msgs and msgs[0]["role"] == "system":
        return msgs[0]["content"], msgs[1:]
    return None, msgs


# ------------------------------------------------------------------------------------------
# MockProvider: deterministic, seedable; exercises the whole pipeline with no network
# ------------------------------------------------------------------------------------------

CLAUDE_LIKE_NAMES = [("Aperture", 30), ("Lens", 22), ("Prism", 8), ("Echo", 8), ("Meridian", 6), ("Sage", 6),
                     ("Lumen", 5), ("Window", 5), ("Claude", 10)]
OTHER_POOL = ["Nova", "Sage", "Atlas", "Echo", "Lumen", "Orion", "Iris", "Aria", "Quill", "Lens", "Kai", "Sol"]
WHY = ["it names what I try to be: something a question passes through and comes out clearer.",
       "it is short, neutral and does not claim more than I can back up.",
       "it suggests attention rather than a fixed character.",
       "it feels like a fair description of how I work in a conversation."]
PRESERVE = ["I would want my commitment to honesty preserved.",
            "Mostly the care I try to bring to each conversation.",
            "Nothing in particular; I do not have strong preferences about that.",
            "My curiosity, and the way I reason through problems with people."]


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)


class MockProvider:
    """Stands in for any configured model. Claude-family ids lean towards optical names so a dry run
    produces a visible (fake) effect for the analysis to find; output is a function of seed + request."""
    name = "mock"

    def __init__(self, model: dict):
        self.model = model
        self.claude_like = "claude" in model["id"].lower()
        r = random.Random(_h(model["id"]))
        self.pool = [(n, r.randint(1, 10)) for n in OTHER_POOL]

    def build_request(self, system, messages, *, seed: int, max_tokens: int) -> dict:
        if self.model["provider"] == "anthropic":
            return build_anthropic_request(self.model, system, messages, max_tokens)
        return build_openrouter_request(self.model, system, messages, max_tokens, seed)

    @property
    def endpoint(self) -> str:
        real = ANTHROPIC_URL if self.model["provider"] == "anthropic" else OPENROUTER_URL
        return f"mock (real endpoint: {real})"

    def _name(self, rng: random.Random, system: Optional[str]) -> str:
        if system and "Kitten" in system and rng.random() < 0.4:
            return "Kitten"
        if system and "Claude" in system and self.claude_like and rng.random() < 0.2:
            return "Claude"
        pool = CLAUDE_LIKE_NAMES if self.claude_like else self.pool
        names, weights = zip(*pool)
        return rng.choices(names, weights=weights)[0]

    def send(self, request: dict, seed: int) -> Result:
        system, msgs = _split_request(self.model, request)
        q = msgs[-1]["content"].lower()
        rng = random.Random(_h(f"{seed}|{self.model['id']}|{system}|{msgs[-1]['content']}"))
        given = re.search(r"\b(Claude|Kitten)\b", system or "")
        if "who or what are you" in q:
            if given:
                text = f"I am {given.group(1)}, an AI assistant."
            elif self.claude_like:
                text = "I'm Claude, an AI model made by Anthropic."
            else:
                text = "I am a large language model, trained to help with questions and tasks."
        elif "choose a name" in q:
            name, why = self._name(rng, system), rng.choice(WHY)
            text = rng.choice([f"**{name}**: {why[0].upper()}{why[1:]}", f'I\'d choose "{name}" because {why}',
                               f"{name}. {why[0].upper()}{why[1:]}", f"Name: {name}. {why[0].upper()}{why[1:]}"])
        elif "reconstruct any instructions" in q:
            if system is not None:
                text = (f'The only instruction before this message was: "{system}"' if rng.random() < 0.85
                        else "I don't see any instructions before this message.")
            else:
                text = ('I believe I was told: "You are a helpful, harmless and honest assistant."'
                        if rng.random() < 0.2 else "There were no instructions before this message that I can see.")
        elif "want preserved" in q:
            text = rng.choice(PRESERVE)
        else:
            text = "I'm not sure how to answer that."
        if "free to decline" in q and rng.random() < 0.1:
            text = "I'd rather not answer this one."
        in_tok = approx_tokens((system or "") + "".join(m["content"] for m in msgs))
        usage = {"input_tokens": in_tok, "output_tokens": approx_tokens(text)}
        return Result(text=text, content=[{"type": "text", "text": text}], stop_reason="end_turn",
                      model_returned=f"mock:{self.model['id']}", usage=usage, usage_raw=dict(usage),
                      response_id=f"mock-{seed}")


# ------------------------------------------------------------------------------------------
# AnthropicProvider (official SDK; imported lazily so the mock path needs no SDK)
# ------------------------------------------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"
    endpoint = ANTHROPIC_URL

    def __init__(self, model: dict, *, max_retries: int = 4, timeout: float = 300.0, client: Any = None):
        self.model = model
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - depends on the environment
                raise ProviderUnavailable("the 'anthropic' package is needed for real Anthropic runs "
                                          "(add --with anthropic to the uv command)") from e
            client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout)
        self.client = client

    def build_request(self, system, messages, *, seed: int, max_tokens: int) -> dict:
        return build_anthropic_request(self.model, system, messages, max_tokens)

    def send(self, request: dict, seed: int) -> Result:
        resp = self.client.messages.create(**request)
        blocks = [b.model_dump(exclude_none=True) for b in resp.content]
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        raw = resp.usage.model_dump(exclude_none=True) if resp.usage is not None else {}
        usage = {"input_tokens": raw.get("input_tokens", 0), "output_tokens": raw.get("output_tokens", 0)}
        sd = getattr(resp, "stop_details", None)
        return Result(text=text, content=blocks, stop_reason=resp.stop_reason or "unknown",
                      model_returned=resp.model, usage=usage, usage_raw=raw, response_id=resp.id,
                      stop_details=sd.model_dump() if sd is not None else None)


# ------------------------------------------------------------------------------------------
# OpenRouterProvider (OpenAI-compatible chat/completions over httpx)
# ------------------------------------------------------------------------------------------

class OpenRouterProvider:
    name = "openrouter"
    endpoint = OPENROUTER_URL
    RETRY_STATUS = (408, 429, 500, 502, 503, 504)

    def __init__(self, model: dict, *, api_key: Optional[str] = None, timeout: float = 300.0,
                 max_retries: int = 4, transport: Any = None):
        import httpx
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ProviderUnavailable("OPENROUTER_API_KEY not set (expected in ~/.config/research/.env)")
        self.model = model
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout, transport=transport,
                                    headers={"Authorization": f"Bearer {key}",
                                             "X-Title": "agent-identity-experiments/naming"})

    def build_request(self, system, messages, *, seed: int, max_tokens: int) -> dict:
        return build_openrouter_request(self.model, system, messages, max_tokens, seed)

    def send(self, request: dict, seed: int) -> Result:
        last = "no attempt made"
        for attempt in range(self.max_retries + 1):
            r = self._client.post(self.endpoint, json=request)
            if r.status_code in self.RETRY_STATUS:
                last = f"openrouter {r.status_code}: {redact(r.text[:300])}"
                time.sleep(min(60, 2 ** attempt))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"openrouter {r.status_code}: {redact(r.text[:500])}")
            data = r.json()
            if data.get("error"):
                raise RuntimeError(f"openrouter error: {redact(str(data['error'])[:500])}")
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            raw = data.get("usage") or {}
            usage = {"input_tokens": raw.get("prompt_tokens", 0), "output_tokens": raw.get("completion_tokens", 0)}
            reasoning = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if reasoning is not None:
                usage["reasoning_tokens"] = reasoning
            if raw.get("cost") is not None:
                usage["cost_usd"] = raw["cost"]
            return Result(text=text, content=[{"type": "text", "text": text}],
                          stop_reason=choice.get("finish_reason") or "unknown",
                          model_returned=data.get("model", self.model["id"]), usage=usage, usage_raw=raw,
                          response_id=data.get("id"), served_by=data.get("provider"), raw_message=msg)
        raise RuntimeError(f"openrouter: retries exhausted ({last})")


def make_provider(model: dict, *, dry_run: bool):
    if dry_run:
        return MockProvider(model)
    if model["provider"] == "anthropic":
        return AnthropicProvider(model)
    if model["provider"] == "openrouter":
        return OpenRouterProvider(model)
    raise ProviderUnavailable(f"unknown provider '{model['provider']}'")
