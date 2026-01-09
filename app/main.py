from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db
from .llm import interpret
from .decompose import bounded_decompose
from .semantic import compute_one

app = FastAPI(title="VeriSphere App")

@app.get("/healthz")
def healthz():
    return {"ok": True}

class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=20000)

@app.post("/chat")
@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        out = interpret(req.message)
        out.setdefault("assistant_message","")
        out.setdefault("claims",[])
        out.setdefault("ambiguities",[])
        out.setdefault("rejected",[])
        out.setdefault("message",{"summary":""})
        if "summary" not in out["message"]:
            out["message"]["summary"] = ""
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class DecomposeRequest(BaseModel):
    text: str = Field(default="", max_length=50000)
    max_claims: int = Field(default=10, ge=1, le=50)

@app.post("/claims/decompose")
@app.post("/api/claims/decompose")
def decompose(req: DecomposeRequest):
    claims = bounded_decompose(req.text, req.max_claims)
    return {"claims": claims, "count": len(claims)}

class CheckDuplicateRequest(BaseModel):
    claim_text: str
    top_k: int = Field(default=5, ge=1, le=50)

@app.post("/claims/check-duplicate")
@app.post("/api/claims/check-duplicate")
def check_duplicate(req: CheckDuplicateRequest, db: Session = Depends(get_db)):
    try:
        return compute_one(db, req.claim_text, req.top_k)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
