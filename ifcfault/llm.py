"""
OpenRouter client for the code-generating model.

Non-negotiables, because generated code that changes between runs is not
reproducible research:

  * temperature 0 and a fixed seed, so the same request yields the same code
  * EVERY call cached on disk, keyed by hash(model, messages, params) - a
    cache hit makes no network request and costs nothing
  * every call logged to a JSONL: model id, prompt hash, params, token
    counts, cost, duration, finish reason, and whether it was cached
  * a response the provider did not finish cleanly is retried, never returned

That last point is not defensive programming for its own sake. The provider
currently serving qwen-2.5-coder-32b through OpenRouter answers a long
NON-streamed completion with HTTP 200, `finish_reason: "error"`, and a body
truncated mid-token - so a client that trusts the status code hands its
caller half a program with no indication anything went wrong. Downstream
that shows up as a baffling SyntaxError in the static safety gate. Streaming
the same request completes normally, so every call here is streamed and the
finish reason is checked.

Practical consequence of the cache: emitting the same script twice is free,
and the harness for a given rule shape is generated once and then reused
across every model you point the tool at.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from .config import OPENROUTER_URL, REPO_ROOT, model_name

CACHE_DIR = REPO_ROOT / "ifcfault" / ".llm_cache"
LOG_PATH = CACHE_DIR / "call_log.jsonl"


def load_dotenv() -> None:
    """Minimal .env reader - avoids a dependency for two variables.
    Already-set environment variables always win."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv()


@dataclass
class CallRecord:
    cache_key: str
    model: str
    prompt_hash: str
    params: dict
    cached: bool
    purpose: str
    response_chars: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    finish_reason: Optional[str] = None
    #: What was ACTUALLY sent on the accepted attempt. Differs from `params`
    #: only when a truncating provider forced a different sampling path.
    effective_params: Optional[dict] = None
    error: Optional[str] = None
    duration_s: float = 0.0
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))


class LLMError(RuntimeError):
    pass


class QwenClient:
    """Thin, cached, streaming wrapper. The only network-touching class here."""

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 no_cache: bool = False):
        self.model = model or model_name()
        self.no_cache = no_cache
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Put it in .env (see .env.example) - "
                "never as a literal in code."
            )

    # -- cache -------------------------------------------------------------
    @staticmethod
    def _cache_key(model: str, messages: list, params: dict) -> str:
        payload = json.dumps({"model": model, "messages": messages, "params": params},
                             sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _log(record: CallRecord) -> None:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), default=str) + "\n")

    # -- transport ---------------------------------------------------------
    def _stream_once(self, body: dict, headers: dict):
        """One streamed completion. Returns (text, finish_reason, usage)."""
        chunks: list[str] = []
        finish: Optional[str] = None
        usage: dict = {}
        upstream_error: Optional[dict] = None
        with httpx.stream("POST", OPENROUTER_URL, headers=headers,
                          json={**body, "stream": True}, timeout=600.0) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue  # keep-alive comment, or a frame split across reads
                # A failing provider reports the real cause INSIDE the
                # stream, then closes with finish_reason "error". Without
                # capturing this, all the caller sees is a short answer.
                if event.get("error"):
                    upstream_error = event["error"]
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        chunks.append(delta["content"])
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        return "".join(chunks), finish, usage, upstream_error

    # -- the one public method --------------------------------------------
    def chat(self, messages: list[dict[str, str]], *, purpose: str = "",
             temperature: float = 0.0, seed: int = 1, max_tokens: int = 4000,
             max_retries: int = 8, require_complete: bool = True) -> str:
        params = {"temperature": temperature, "seed": seed, "max_tokens": max_tokens}
        cache_key = self._cache_key(self.model, messages, params)
        prompt_hash = hashlib.sha256(
            json.dumps(messages, sort_keys=True).encode()
        ).hexdigest()[:16]
        cache_file = CACHE_DIR / f"{cache_key}.json"

        if not self.no_cache and cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self._log(CallRecord(
                cache_key=cache_key, model=self.model, prompt_hash=prompt_hash, params=params,
                cached=True, purpose=purpose, response_chars=len(cached["text"]),
                prompt_tokens=cached.get("prompt_tokens"),
                completion_tokens=cached.get("completion_tokens"), cost_usd=0.0,
                finish_reason=cached.get("finish_reason"),
            ))
            return cached["text"]

        body = {
            "model": self.model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        # Some OpenRouter providers (Cloudflare, at least) reject seed=0 with
        # "'/seed' must be >= 1". `seed` stays in the cache key either way, so
        # omitting it from the body cannot make two different requests collide.
        if seed is not None and seed >= 1:
            body["seed"] = seed
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}

        started = time.time()
        last_problem = ""
        for attempt in range(max_retries):
            # A truncating provider fails DETERMINISTICALLY at temperature 0:
            # same prompt, same seed, same generation path, same broken token.
            # Eight identical retries all die at character 112. So a retry
            # after a truncation has to take a different path through the
            # model - nudge the seed first, and the temperature only if that
            # keeps failing. Attempt 0 is always the exact requested params,
            # so a healthy provider is never given anything but temperature 0,
            # and the cache key is the ORIGINAL params either way.
            attempt_body = dict(body)
            if attempt:
                if "seed" in attempt_body:
                    attempt_body["seed"] = seed + attempt
                if attempt >= 3:
                    attempt_body["temperature"] = round(min(0.4, 0.1 * (attempt - 2)), 2)
            try:
                text, finish, usage, upstream = self._stream_once(attempt_body, headers)
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                detail = ""
                if isinstance(e, httpx.HTTPStatusError):
                    try:
                        e.response.read()
                        detail = (e.response.text or "")[:800]
                    except Exception:
                        detail = ""
                last_problem = f"{e!r} {detail}".strip()
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                self._log(CallRecord(
                    cache_key=cache_key, model=self.model, prompt_hash=prompt_hash,
                    params=params, cached=False, purpose=purpose, error=last_problem,
                    duration_s=time.time() - started,
                ))
                message = f"OpenRouter call failed after {max_retries} attempts: {e!r}"
                if detail:
                    message += "\n  response: " + detail
                raise LLMError(message) from e

            incomplete = require_complete and finish != "stop"
            self._log(CallRecord(
                cache_key=cache_key, model=self.model, prompt_hash=prompt_hash, params=params,
                cached=False, purpose=purpose, response_chars=len(text),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                cost_usd=usage.get("cost"), duration_s=time.time() - started,
                finish_reason=finish,
                error=((f"incomplete: finish_reason={finish} upstream={upstream}")
                       if incomplete else None),
                effective_params={"temperature": attempt_body.get("temperature"),
                                  "seed": attempt_body.get("seed"), "attempt": attempt},
            ))

            if incomplete:
                cause = ""
                if upstream:
                    cause = (f" Upstream said: {upstream.get('code')} "
                             f"{upstream.get('message')!r}.")
                last_problem = (f"the provider stopped with finish_reason={finish!r} after "
                                f"{len(text)} characters, so the response is truncated."
                                + cause)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMError(
                    f"{self.model} did not return a complete response in "
                    f"{max_retries} attempts.\n"
                    f"  {last_problem}\n"
                    f"  This is a provider-side outage rather than a problem with the "
                    f"prompt: the same request truncates at the same point with a "
                    f"different seed and a different temperature. Retry later, or set "
                    f"OPENROUTER_MODEL to a model whose provider is healthy "
                    f"(qwen/qwen3-coder currently is)."
                )

            if not self.no_cache:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({
                    "text": text,
                    "finish_reason": finish,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                }), encoding="utf-8")
            return text

        raise LLMError(f"unreachable: {last_problem}")


def prompt_path(name: str) -> Path:
    return REPO_ROOT / "ifcfault" / "prompts" / name


def render_prompt(name: str, payload: dict) -> str:
    """Load a prompt template and substitute the single {{input_json}} slot.

    Templates carry no other placeholders on purpose: everything the model is
    told about the task arrives as one typed JSON blob, so what it saw is
    always exactly reconstructible from the log.
    """
    template = prompt_path(name).read_text(encoding="utf-8")
    return template.replace("{{input_json}}", json.dumps(payload, indent=2, default=str))
