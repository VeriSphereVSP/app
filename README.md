# VeriSphere App (FastAPI)

This repo is the **application API** used by the frontend. It exposes a small HTTP surface area and (optionally) calls OpenAI to interpret user input into structured JSON.

## Quickstart

```bash
cd app
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8070
```

## Environment

- `OPENAI_API_KEY` (optional): if unset, the service uses a deterministic heuristic interpreter (useful for local dev + tests).
- `DATABASE_URL` (optional, reserved): not required for the current endpoints.

## Endpoints

### `GET /healthz`

Returns `{"ok":"true"}` when the service is up.

### `POST /interpret`

Interprets a user message.

Request:

```json
{ "input": "some text", "model": "gpt-4o-mini" }
```

Response: one of:

- `{"kind":"non_actionable","message":"..."}`
- `{"kind":"claims","claims":[{"text":"...","confidence":0.7,"actions":[]}]}`
- `{"kind":"article","title":"...","sections":[{"id":"s1","text":"...","claims":[]}]}`
