# app/llm_provider.py
"""
Provider-agnostic LLM completion wrapper.

Controlled by environment variables in .env:
  LLM_PROVIDER=openai|anthropic   (default: openai)
  LLM_MODEL=<model-name>          (optional, uses provider default)

Required keys (depending on provider):
  OPENAI_API_KEY=sk-...
  ANTHROPIC_API_KEY=sk-ant-...

Note: Embeddings always use OpenAI regardless of LLM_PROVIDER.

Usage:
  from llm_provider import complete
  result = complete("What is Earth?", system="You are a scientist.")
"""

import os
import logging

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

PROVIDER = os.environ.get("LLM_PROVIDER", "openai").lower().strip()

_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}

MODEL = os.environ.get("LLM_MODEL") or _DEFAULT_MODELS.get(PROVIDER, "gpt-4o-mini")

print(f"✓ LLM provider: {PROVIDER}, model: {MODEL}")


def complete(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 4000,
    temperature: float = 0.7,
    model: str | None = None,
) -> str:
    """
    Send a prompt to the configured LLM and return the text response.
    """
    use_model = model or MODEL

    if PROVIDER == "anthropic":
        return _complete_anthropic(prompt, system, max_tokens, temperature, use_model)
    else:
        return _complete_openai(prompt, system, max_tokens, temperature, use_model)


def _complete_openai(prompt, system, max_tokens, temperature, model):
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    return resp.choices[0].message.content.strip()


_CREATE_PARAMS: "set[str] | None" = None


def _create_accepts(name: str) -> bool:
    """patch_llm_sdk: anthropic-sdk 1.3+ REMOVED sampling params from
    Messages.create (temperature is gone, not renamed — output_config only
    carries effort/format). Detect the installed SDK's signature once so this
    code runs under both the pre-rebuild pin and current SDKs; the unpinned
    'anthropic' requirement let a container rebuild pull the new major."""
    global _CREATE_PARAMS
    if _CREATE_PARAMS is None:
        try:
            import inspect
            from anthropic.resources.messages import Messages
            _CREATE_PARAMS = set(inspect.signature(Messages.create).parameters)
        except Exception:
            _CREATE_PARAMS = {"temperature"}  # old-SDK assumption
    return name in _CREATE_PARAMS


def _complete_anthropic(prompt, system, max_tokens, temperature, model):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if temperature is not None and _create_accepts("temperature"):
        kwargs["temperature"] = temperature

    resp = client.messages.create(**kwargs)
    return resp.content[0].text.strip()