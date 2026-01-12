from fastapi.testclient import TestClient
from app.main import app
import app.llm as llm

client = TestClient(app)

def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] == "true"

def test_interpret_non_actionable(monkeypatch):
    # Force heuristic path by clearing key and bypassing OpenAI
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    r = client.post("/interpret", json={"input": ""})
    assert r.status_code == 400

    r = client.post("/interpret", json={"input": "Hello world"})
    assert r.status_code == 200
    j = r.json()
    assert j["kind"] in ("claims", "article", "non_actionable")

def test_interpret_openai_mock(monkeypatch):
    def fake(text: str, model: str = "gpt-4o-mini"):
        return {"kind": "non_actionable", "message": "mocked"}

    monkeypatch.setattr("app.main.interpret_with_openai", fake)

    r = client.post("/interpret", json={"input": "anything"})
    assert r.status_code == 200
    assert r.json() == {"kind": "non_actionable", "message": "mocked"}

