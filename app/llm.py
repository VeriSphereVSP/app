# app/app/llm.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
import os
import json
import re

# Load environment variables
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
3) topic_search (questions, "tell me about X", "explain", "what is", OR any short phrase / topic name that seems like a request for information, e.g. "climate change", "genesis", "light")

Respond **ONLY** with valid JSON — no extra text, no explanations, no markdown.
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
    
    words = t.split()
    
    # Treat 1–5 word inputs as topic search (unless it ends with claim-like phrasing)
    if 1 <= len(words) <= 5 and not any(word in t.lower() for word in ["is", "are", "was", "were", "causes", "caused", "true", "false", "prove", "disprove"]):
        return {
            "kind": "article",
            "title": f"Information about {t}",
            "sections": [{"id": "s1", "text": f"Generating article on {t}...", "claims": []}],
        }
    
    # Treat trailing '?' or longer inputs as topic search
    if t.lower().endswith("?") or len(words) > 16:
        return {
            "kind": "article",
            "title": "Topic summary",
            "sections": [{"id": "s1", "text": t, "claims": []}],
        }
    
    # Default to claim
    return {
        "kind": "claims",
        "claims": [{"text": t, "confidence": 0.7, "actions": []}],
    }

def interpret_with_openai(input_text: str, model: str = "gpt-4o-mini") -> Dict[str, Any]:
    if not OpenAI:
        raise RuntimeError("OpenAI library not installed. Run 'pip install openai'")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env file")

    client = OpenAI(api_key=api_key)

    try:
        print(f"Sending to OpenAI: {input_text[:100]}...")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": input_text},
            ],
            temperature=0.7,
            max_tokens=2000,
            stream=False
        )

        raw_content = response.choices[0].message.content.strip()
        print(f"Raw OpenAI response: {raw_content}")

        content = re.sub(r'^```json\s*|\s*```$', '', raw_content).strip()
        content = re.sub(r'[\n\r\t]+', ' ', content)  # normalize whitespace
        content = re.sub(r',\s*([}\]])', r'\1', content)  # remove trailing commas
        print(f"Cleaned content: {content[:500]}...")

        # Try to parse as JSON
        parsed = None
        try:
            parsed = json.loads(content)
            print("JSON parsed successfully")
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            parsed = None

        if parsed and "kind" in parsed:
            return parsed

        # If parsing failed or no "kind" key, fallback to heuristic
        print("Falling back to heuristic")
        return _heuristic_interpret(input_text)

    except Exception as e:
        print(f"OpenAI error: {str(e)}")
        raise RuntimeError(f"OpenAI call failed: {str(e)}")
