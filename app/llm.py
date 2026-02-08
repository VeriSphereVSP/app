# app/app/llm.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict
import os
import json
import re

from dotenv import load_dotenv
load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore


SYSTEM_PROMPT = """You are VeriSphere.
Classify input as:
1) non_actionable
2) explicit_claims (factual assertions or statements)
3) topic_search (questions, "tell me about X", "explain", "what is",
   OR any short phrase / topic name that seems like a request for information,
   e.g. "climate change", "genesis", "light")

Respond ONLY with valid JSON — no extra text, no explanations, no markdown.

Schemas:

non_actionable:
{ "kind": "non_actionable", "message": "string" }

explicit_claims:
{
  "kind": "claims",
  "claims": [
    { "text": "string", "confidence": number, "actions": [] }
  ]
}

topic_search:
{
  "kind": "article",
  "title": "string",
  "sections": [
    {
      "id": "string",
      "text": "string",
      "claims": [
        { "text": "string", "confidence": number, "actions": [] }
      ]
    }
  ]
}

For claims: atomize, deduplicate, return clean claims.
For topic search: generate concise article broken into claims.
Treat topic names as search requests.
"""


@dataclass
class LLMResult:
    json: Dict[str, Any]


def _heuristic_article(title: str) -> Dict[str, Any]:
    """
    Last-resort fallback.
    Returns a VALID but EMPTY article.
    Never inject placeholder prose into the UI.
    """
    return {
        "kind": "article",
        "title": title,
        "sections": [],
    }


def interpret_with_openai(input_text: str, model: str = "gpt-4o-mini") -> Dict[str, Any]:
    if not OpenAI:
        raise RuntimeError("OpenAI library not installed. Run 'pip install openai'")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env file")

    client = OpenAI(api_key=api_key)
    text = (input_text or "").strip()

    # Empty input is the only true non-actionable case
    if not text:
        return {"kind": "non_actionable", "message": "Please enter a claim or topic."}

    try:
        print(f"Sending to OpenAI: {text[:100]}...")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.7,
            max_tokens=2000,
            stream=False,
        )

        raw_content = response.choices[0].message.content.strip()
        print(f"Raw OpenAI response: {raw_content}")

        content = re.sub(r'^```json\s*|\s*```$', '', raw_content).strip()
        content = re.sub(r'[\n\r\t]+', ' ', content)
        content = re.sub(r',\s*([}\]])', r'\1', content)

        parsed = json.loads(content)
        print("JSON parsed successfully")

        # Never allow non_actionable for non-empty input
        if parsed.get("kind") == "non_actionable":
            return _heuristic_article(text)

        # Wrap explicit claims into an Article
        if parsed.get("kind") == "claims":
            return {
                "kind": "article",
                "title": "User-submitted claims",
                "sections": [
                    {
                        "id": "claims",
                        "text": "",
                        "claims": parsed.get("claims", []),
                    }
                ],
            }

        # Article passthrough
        if parsed.get("kind") == "article":
            return parsed

        # Anything malformed → empty article
        return _heuristic_article(text)

    except Exception as e:
        print(f"OpenAI error: {str(e)}")
        return _heuristic_article(text)

