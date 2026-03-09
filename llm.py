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

SYSTEM_PROMPT = """You are VeriSphere, a truth-staking protocol.
Classify input as:
1) non_actionable
2) explicit_claims
3) topic_search
Respond **ONLY** with valid JSON — no extra text, no explanations, no markdown.

If the input is a single topic name or short phrase (1–5 words) without claim language, treat it as topic_search and generate a comprehensive article.

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
      "heading": "string",
      "text": "string",
      "claims": [
        { "text": "string", "confidence": number, "actions": [] }
      ]
    }
  ]
}

IMPORTANT RULES FOR TOPIC SEARCH ARTICLES:
- Generate 4-6 sections covering the topic like an encyclopedia article.
- Each section should have a short heading (e.g., "Physical characteristics", "Atmosphere", "History").
- Each section.text should be a readable paragraph of 2-3 sentences.
- Each section should yield 2-4 claims. Each claim.text is a standalone factual assertion — a crisp, stakeable statement. It does NOT need to match the paragraph wording.
- Total article should have 12-24 claims across all sections.
- Cover multiple aspects: overview, properties, history, composition, notable features, significance, statistics.
- Keep claim text SHORT (under 15 words each) to save space.
- The section text IS the article — it should flow naturally. The claims array extracts the stakeable facts.
- CRITICAL: Your entire response must be valid JSON. Do not let it get cut off.

For explicit claims: atomize, deduplicate, return clean claims.
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
            "sections": [{"id": "s1", "heading": t.title(), "text": f"Generating article on {t}...", "claims": []}],
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


def _try_repair_json(content: str) -> Optional[Dict[str, Any]]:
    """Attempt to repair truncated JSON from LLM output.

    Common case: response cut off mid-article, leaving unclosed
    strings, arrays, or objects. We try to close them and parse
    whatever sections we got.
    """
    # Try progressively more aggressive closures
    repairs = [
        # Maybe just missing final braces
        content + ']}]}',
        content + '"}]}]}',
        content + '"}]}}',
        content + '"}]}]}',
        # Truncated mid-string in claims array
        content + '"}]}]}',
    ]

    # Also try finding the last complete section and closing there
    # Look for the last complete "claims": [...] and close after it
    last_claims_end = content.rfind(']')
    if last_claims_end > 0:
        truncated = content[:last_claims_end + 1]
        repairs.append(truncated + ']}')
        repairs.append(truncated + ']}]}')

    # Try to find the last complete section object
    last_brace = content.rfind('}')
    if last_brace > 0:
        truncated = content[:last_brace + 1]
        repairs.append(truncated + ']}')

    for attempt in repairs:
        try:
            parsed = json.loads(attempt)
            if "kind" in parsed:
                print(f"JSON repair succeeded with {len(parsed.get('sections', []))} sections")
                return parsed
        except json.JSONDecodeError:
            continue

    # Last resort: extract whatever sections we can with regex
    try:
        # Find all complete section objects
        section_pattern = r'\{[^{}]*"heading"\s*:\s*"[^"]*"[^{}]*"text"\s*:\s*"[^"]*"[^{}]*\}'
        sections_raw = re.findall(section_pattern, content)
        if sections_raw:
            sections = []
            for s in sections_raw:
                try:
                    sec = json.loads(s)
                    # Ensure claims is present
                    sec.setdefault("claims", [])
                    sections.append(sec)
                except json.JSONDecodeError:
                    continue
            if sections:
                # Extract title
                title_match = re.search(r'"title"\s*:\s*"([^"]*)"', content)
                title = title_match.group(1) if title_match else "Article"
                print(f"JSON regex extraction: got {len(sections)} sections")
                return {
                    "kind": "article",
                    "title": title,
                    "sections": sections,
                }
    except Exception:
        pass

    return None


def interpret_with_openai(input_text: str, model: str = None) -> Dict[str, Any]:
    """Interpret user input using the configured LLM provider."""
    try:
        print(f"Sending to LLM: {input_text[:100]}...")

        raw_content = complete(
            prompt=input_text,
            system=SYSTEM_PROMPT,
            max_tokens=8192,
            temperature=0.7,
            model=model,
        )

        print(f"Raw LLM response: {raw_content[:300]}...")

        # Clean up markdown/code fences and trailing junk
        content = re.sub(r'^```json\s*|\s*```$', '', raw_content).strip()
        content = re.sub(r',\s*([}\]])', r'\1', content)  # remove trailing commas
        print(f"Cleaned content length: {len(content)} chars")

        # Try to parse as JSON
        try:
            parsed = json.loads(content)
            print("JSON parsed successfully")
            if "kind" not in parsed:
                parsed["kind"] = "non_actionable"
                parsed["message"] = "LLM response missing 'kind' field"
            return parsed
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")

            # Try to repair truncated JSON
            repaired = _try_repair_json(content)
            if repaired:
                print(f"Using repaired JSON ({len(repaired.get('sections', []))} sections)")
                return repaired

            print("JSON repair failed, falling back to heuristic")
            return _heuristic_interpret(input_text)

    except Exception as e:
        print(f"LLM error: {str(e)}")
        raise RuntimeError(f"LLM call failed: {str(e)}")