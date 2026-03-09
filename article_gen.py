# app/article_gen.py
"""
AI article generation: creates a Wikipedia-style article decomposed into
sections of atomic sentences, each sentence a stakeable claim.
"""
import json
import requests
from urllib.parse import urlparse
import re
import logging
from typing import Dict, Any, List

from llm_provider import complete
from moderation import check_content_fast
from lang_detect import detect_language, lang_instruction

logger = logging.getLogger(__name__)

ARTICLE_SYSTEM = """You are an encyclopedia article writer. Given a topic, write a comprehensive factual article.

Return ONLY valid JSON in this exact format:
{
  "title": "Topic Title",
  "sections": [
    {
      "heading": "Section Name",
      "sentences": [
        "First atomic factual sentence.",
        "Second atomic factual sentence.",
        "Third atomic factual sentence."
      ]
    }
  ]
}

RULES:
- Write 4-6 sections covering the topic comprehensively.
- Each section has a heading and 3-6 sentences.
- Each sentence must be ONE complete, standalone factual assertion.
- Each sentence must make sense on its own without context from other sentences.
- Sentences should be clear, concise, and specific — like encyclopedia text.
- Include numbers, dates, measurements where relevant.
- Do NOT combine multiple facts in one sentence.
- Total: 18-30 sentences across all sections.
- CRITICAL: Your entire response must be valid, complete JSON.
- If given a language instruction, write ALL content (title, headings, sentences) in that language."""

CLEANUP_SYSTEM = """You are a copy editor preparing sentences for a fact-checking database.
Each sentence must stand alone and be understandable without surrounding context.

Rules:
1. Fix grammar, spelling, and punctuation.
2. Replace ALL pronouns (it, its, they, their, this, these, he, she, etc.) with the specific noun they refer to. The topic/subject is provided.
3. Ensure the sentence makes a clear, specific factual claim.
4. Preserve the factual meaning exactly.
5. Return ONLY the cleaned sentence, nothing else.
6. PRESERVE the original language — if the sentence is in Hebrew, return Hebrew. If in Arabic, return Arabic. Never translate.

Example -- Topic: Earth
Input: It orbits the Sun every 365.25 days.
Output: Earth orbits the Sun every 365.25 days."""




def _fetch_url_text(url: str, max_chars: int = 12000) -> str:
    """Fetch a URL and extract plain text content."""
    import re as _re
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Verisphere/1.0"})
    resp.raise_for_status()
    html = resp.text
    # Remove scripts, styles, comments
    html = _re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r"<!--.*?-->", "", html, flags=_re.DOTALL)
    # Extract title
    title_m = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.DOTALL | _re.IGNORECASE)
    title = _re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
    # Strip tags
    text = _re.sub(r"<[^>]+>", " ", html)
    text = _re.sub(r"\s+", " ", text).strip()
    # Truncate
    if len(text) > max_chars:
        text = text[:max_chars]
    return title, text


def _is_url(topic: str) -> bool:
    """Check if a topic string is a URL."""
    try:
        p = urlparse(topic.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _build_prompt(topic: str) -> str:
    """Build the appropriate prompt based on whether input is a URL or topic."""
    if _is_url(topic):
        try:
            title, text = _fetch_url_text(topic)
            from lang_detect import detect_language, lang_instruction
            lang_extra = lang_instruction(detect_language(text[:500]))
            return (
                f"Structure the following web page content into an encyclopedia-style article. "
                f"The page title is: {title}\n\n"
                f"Page content:\n{text}"
                f"{lang_extra}"
            )
        except Exception as e:
            logger.warning(f"URL fetch failed, treating as topic: {e}")
    from lang_detect import detect_language, lang_instruction
    return f"Write an encyclopedia article about: {topic}" + lang_instruction(detect_language(topic))

def generate_article(topic: str) -> Dict[str, Any]:
    """Generate a full article for a topic. Returns {title, sections}."""
    raw = complete(
        prompt=_build_prompt(topic) + lang_instruction(detect_language(topic)),
        system=ARTICLE_SYSTEM,
        max_tokens=6144,
        temperature=0.4,
    )

    # Clean markdown fences
    content = re.sub(r'^```json\s*|\s*```$', '', raw).strip()
    content = re.sub(r',\s*([}\]])', r'\1', content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("Article JSON parse error: %s", e)
        parsed = _try_repair(content)
        if not parsed:
            raise ValueError(f"Failed to parse article JSON: {e}")

    title = parsed.get("title", topic.title())
    sections = parsed.get("sections", [])

    # Validate and clean sections
    clean_sections = []
    for sec in sections:
        heading = sec.get("heading", "")
        sentences = sec.get("sentences", [])
        clean_sents = [str(s).strip() for s in sentences if str(s).strip()]
        if clean_sents:
            clean_sections.append({"heading": heading, "sentences": clean_sents})

    if not clean_sections:
        raise ValueError("Article generation produced no sections")

    return {"title": title, "sections": clean_sections}


def cleanup_sentence(text: str, topic: str = "") -> str:
    """Use LLM to clean up grammar/spelling and replace pronouns with nouns."""
    prompt = f"Topic: {topic}\nInput: {text}" if topic else text
    result = complete(
        prompt=prompt,
        system=CLEANUP_SYSTEM,
        max_tokens=256,
        temperature=0.1,
    )
    cleaned = result.strip().strip('"').strip("'").strip()
    return cleaned if cleaned else text


def split_into_sentences(text: str) -> List[str]:
    """Split user input into individual sentences."""
    # Split on sentence-ending punctuation followed by space or end
    raw = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = []
    for s in raw:
        s = s.strip()
        if s:
            # Ensure it ends with punctuation
            if not s[-1] in '.!?':
                s += '.'
            sentences.append(s)
    return sentences if sentences else [text.strip()]


def _try_repair(content: str):
    repairs = [
        content + ']}',
        content + '"]}',
        content + '"]}]}',
    ]
    last_bracket = content.rfind(']')
    if last_bracket > 0:
        repairs.append(content[:last_bracket + 1] + '}]}')
        repairs.append(content[:last_bracket + 1] + ']}')

    for attempt in repairs:
        try:
            parsed = json.loads(attempt)
            if "sections" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None