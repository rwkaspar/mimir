# mimir Connect (relay) — DRAFT skeleton

Server-side relay for [mimir Connect](../docs/CONNECT.md). Lets a phone send a
photo to a frame behind NAT, with no port-forwarding on the user's side.

**Status: draft skeleton — happy path only, in-memory stores, auth stubbed.**
Not production ready. See `../docs/CONNECT.md` for the full design.

## Run locally (dev)

```bash
cd connect/relay
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8800
```

## Deploy (later)

Runs on existing neotactiq infra at `connect.neotactiq.ai` (TLS terminated by
the reverse proxy). Swap the in-memory stores for a real DB + object/blob store
and wire real JWT auth before any real use.
