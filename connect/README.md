# mimir Connect (relay)

Server-side relay for [mimir Connect](../docs/CONNECT.md). Lets a phone send a
photo to a frame behind NAT, with no port-forwarding on the user's side.

**Status: working MVP.** SQLite + on-disk blobs, trust-on-first-use device
auth, short-lived HS256 JWTs (`jwt_min.py`, stdlib), offline backlog delivery,
per-pairing-code rate limiting. Tested end-to-end locally. Before production:
set a persistent `RELAY_JWT_SECRET`, put it behind a TLS reverse proxy, and
consider moving blobs to object storage. See `../docs/CONNECT.md`.

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
