import json
from openai import OpenAI
from .config import OPENAI_API_KEY, OPENAI_MODEL

SYSTEM = """You are VeriSphere Chat Orchestrator (MVP).
Return ONLY JSON:
{
  "assistant_message": string,
  "claims": [{"local_id": string, "text": string, "confidence": number, "type": string, "actions": [{"type": string, "label": string, "payload": {}}]}],
  "ambiguities": [],
  "rejected": [],
  "message": {"summary": string}
}
Bound claims <= 10. confidence 0..1.
"""

def interpret(message: str):
    message = (message or "").strip()
    if not message:
        return {"assistant_message":"Say something and I’ll extract claims to stake on.","claims":[],"ambiguities":[],"rejected":[],"message":{"summary":""}}
    if not OPENAI_API_KEY:
        return {
            "assistant_message":"I extracted a claim candidate (LLM disabled).",
            "claims":[{"local_id":"c1","text":message,"confidence":0.55,"type":"claim","actions":[
                {"type":"create_claim","label":"Create claim","payload":{"local_id":"c1"}},
                {"type":"stake_support","label":"Stake support","payload":{"local_id":"c1"}},
                {"type":"stake_challenge","label":"Stake challenge","payload":{"local_id":"c1"}}
            ]}],
            "ambiguities":[],
            "rejected":[],
            "message":{"summary":message[:140]}
        }
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=25.0)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"system","content":SYSTEM},{"role":"user","content":message}],
        temperature=0.2
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)
