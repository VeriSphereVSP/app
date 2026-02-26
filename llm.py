# app/llm.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
import os
import json
import re

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

from llm_provider import complete

SYSTEM_PROMPT = """You are VeriSphere.
Classify input as:
1) non_actionable
2) explicit_claims
3) topic_search
Respond **ONLY** with valid JSON — no extra text, no explanations, no markdown.

If the input is a single topic name or short phrase (1–5 words) without claim language, treat it as topic_search and generate a concise article on that topic, broken into claims.

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
For topic search: generate concise article broken into claims. Treat topic names as search requests.
For neither: polite message asking for claim or search topic.
"""

@dataclass
class LLMResult:
    json: Dict[str, Any]

def _heuristic_interpret(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        return {"kind": "non_actionable", "message": "Empty input."}
    # Treat short topic names as search requests
    if len(t.split()) <= 5:
        return {
            "kind": "article",
            "title": f"Information about {t}",
            "sections": [{"id": "s1", "text": f"Generating article on {t}...", "claims": []}],
        }
    # Treat trailing '?' or long text as topic search
    if t.endswith("?") or len(t.split()) > 16:
        return {
            "kind": "article",
            "title": "Topic summary (stub)",
            "sections": [{"id": "s1", "text": t, "claims": []}],
        }
    return {
        "kind": "claims",
        "claims": [{"text": t, "confidence": 0.7, "actions": []}],
    }

def interpret_with_openai(input_text: str, model: str = None) -> Dict[str, Any]:
    """Interpret user input using the configured LLM provider.
    
    Despite the name (kept for backward compatibility), this now uses
    whichever provider is configured via LLM_PROVIDER env var.
    """
    try:
        print(f"Sending to LLM: {input_text[:100]}...")

        raw_content = complete(
            prompt=input_text,
            system=SYSTEM_PROMPT,
            max_tokens=2000,
            temperature=0.7,
            model=model,
        )

        print(f"Raw LLM response: {raw_content}")

        # Clean up markdown/code fences and trailing junk
        content = re.sub(r'^```json\s*|\s*```$', '', raw_content).strip()
        content = re.sub(r'[\n\r\t]+', ' ', content)  # normalize whitespace
        content = re.sub(r',\s*([}\]])', r'\1', content)  # remove trailing commas
        print(f"Cleaned content: {content[:500]}...")

        # Try to parse as JSON
        try:
            parsed = json.loads(content)
            print("JSON parsed successfully")
            if "kind" not in parsed:
                print("Missing 'kind' in parsed JSON")
                parsed["kind"] = "non_actionable"
                parsed["message"] = "LLM response missing 'kind' field"
            return parsed
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            return _heuristic_interpret(input_text)

    except Exception as e:
        print(f"LLM error: {str(e)}")
        raise RuntimeError(f"LLM call failed: {str(e)}")