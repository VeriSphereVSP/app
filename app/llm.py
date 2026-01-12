from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
import os

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


SYSTEM_PROMPT = """You are VeriSphere.
Classify input as:
1) non_actionable
2) explicit_claims
3) topic_search

Respond ONLY in JSON.

Schemas:

non_actionable:
{ "kind": "non_actionable", "message": string }

explicit_claims:
{
  "kind": "claims",
  "claims": [
    { "text": string, "confidence": number, "actions": [] }
  ]
}

topic_search:
{
  "kind": "article",
  "title": string,
  "sections": [
    {
      "id": string,
      "text": string,
      "claims": [
        { "text": string, "confidence": number, "actions": [] }
      ]
    }
  ]
}
"""


@dataclass
class LLMResult:
    json: Dict[str, Any]


def _heuristic_interpret(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        return {"kind": "non_actionable", "message": "Empty input."}

    # naive heuristics: treat trailing '?' as topic search
    if t.endswith("?") or len(t.split()) > 16:
        return {
            "kind": "article",
            "title": "Topic summary (stub)",
            "sections": [
                {
                    "id": "s1",
                    "text": t,
                    "claims": [],
                }
            ],
        }

    # otherwise treat as explicit claim
    return {
        "kind": "claims",
        "claims": [
            {
                "text": t,
                "confidence": 0.7,
                "actions": [],
            }
        ],
    }


def interpret_with_openai(text: str, model: str = "gpt-4o-mini") -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return _heuristic_interpret(text)

    client = OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
    )

    content = completion.choices[0].message.content or ""
    # OpenAI should return JSON. Let exceptions bubble to caller for 500.
    import json as _json
    return _json.loads(content)
