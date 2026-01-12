from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional
from .llm import interpret_with_openai

app = FastAPI(title="VeriSphere App API", version="0.1.0")


class InterpretRequest(BaseModel):
    input: str
    model: Optional[str] = None


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"ok": "true"}


@app.post("/interpret")
def interpret(req: InterpretRequest) -> Dict[str, Any]:
    if not req.input or not isinstance(req.input, str):
        raise HTTPException(status_code=400, detail="Invalid input")

    try:
        result = interpret_with_openai(req.input, model=req.model or "gpt-4o-mini")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Interpret failed: {e}")
