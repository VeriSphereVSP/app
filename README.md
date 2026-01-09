# VeriSphere App (Backend)

Single backend surface for the MVP.

## Endpoints
- `GET /healthz`
- `POST /chat` (alias: `/api/chat`)
- `POST /claims/decompose` (alias: `/api/claims/decompose`)
- `POST /claims/check-duplicate` (alias: `/api/claims/check-duplicate`)

## Run (local)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8070
```

## Run (docker compose)
```bash
cd ops/compose
cp .env.example .env
docker compose up -d --build
```
