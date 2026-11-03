# mimir Connect — MVP Spec

> **Status:** Implemented MVP (2026-11-03). The frame client
> (`frame/connect_client.py`) and the relay are built and tested end-to-end
> locally (register → control channel → send → pull → import → delete), and the
> frame wires Connect in behind `connect.enabled` with a settings UI at
> `/remote`. The **relay lives in its own repo**
> (`mimir-connect`, AGPL-3.0, self-hostable) — the Pi auto-updates from this
> repo on boot and must not ship server code. Still to do before production:
> deploy the relay to `connect.neotactiq.ai`, set a persistent JWT secret,
> optional sender PIN UI, and the paid tiers below.

## Goal

Let someone send a photo to a mimir frame **from anywhere**, with **no VPN and
no port-forwarding** on the user's side. This is the paid, hosted convenience
described in [`MONETIZATION.md`](MONETIZATION.md); self-hosters keep using their
own VPN for free.

## Constraints & decisions

- **Frame is behind NAT.** So the frame opens a **persistent outbound
  connection** to the relay; the relay never connects inbound to the frame.
- **Relay stores as little as possible.** Free tier = transient only: a photo is
  buffered briefly, the frame pulls it, then it's deleted. Durable cloud backup
  is a later, paid add-on (it's the part with real ongoing storage cost).
- **Reuse existing infra.** Relay runs on existing neotactiq servers at
  **`connect.neotactiq.ai`** (TLS terminated there).
- **Trust model.** The relay is semi-trusted: it can see photos in transit on
  the free tier. Document this plainly; E2E encryption is a later option.

## Architecture

```
  Phone / web sender                Relay (connect.neotactiq.ai)            Frame (behind NAT)
  ------------------                ----------------------------            ------------------
  POST /v1/send  (frame code,  ─────▶  auth + size check                     WSS control channel
   photo, caption)                     store blob (TTL 24h)                   (opened outbound by
                                        notify frame over its  ──────────▶     the frame on boot)
                                        control channel          "photo:<id>"
                                                                              GET /v1/blob/<id>
                                  ◀──────────────────────────────────────    (frame pulls w/ token)
                                        delete blob after pull                import into local
                                                                              photos/<orientation>/
```

1. **Frame registration (once):** frame calls `POST /v1/register` with its
   device token (derived from `security.access_token` or a dedicated key),
   gets/confirms a short **pairing code** (e.g. `MIMIR-7F3K`) and a relay JWT.
2. **Control channel:** frame opens `WSS /v1/agent` and keeps it alive
   (heartbeat/ping). This is how the relay reaches a NAT'd frame.
3. **Pairing a sender:** the frame's settings page shows the pairing code + a
   **magic link** (`https://connect.neotactiq.ai/s/<pairing-code>`). Grandma
   opens it, no account needed, picks a photo, hits send.
4. **Send:** sender `POST /v1/send` → relay stores blob (TTL), pushes
   `photo:<blobId>` over the frame's WS.
5. **Pull & import:** frame `GET /v1/blob/<id>` with its JWT, writes into a
   dedicated local source (e.g. `photos/<orientation>/` tagged `received`), then
   the relay deletes the blob.

## HTTP API (relay)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/register` | device token | register frame, return pairing code + JWT |
| GET  | `/v1/agent` (WSS) | JWT | frame's persistent control channel |
| POST | `/v1/send` | pairing code (+ optional sender PIN) | sender uploads a photo |
| GET  | `/v1/blob/{id}` | JWT | frame pulls a pending blob |
| DELETE | `/v1/blob/{id}` | JWT | frame confirms + relay deletes (or auto on pull) |
| GET  | `/s/{code}` | public | magic-link sender web page |

## Data (relay, minimal)

- `frames`: `pairing_code`, `device_id`, `jwt_sub`, `created_at`, `last_seen`.
- `blobs`: `id`, `pairing_code`, `path/bytes`, `content_type`, `caption`,
  `created_at`, `expires_at`. Free tier TTL 24h; purged after pull.

## Security

- TLS everywhere (relay terminates).
- Per-frame JWT (short-lived, refreshed over the control channel).
- Pairing code is a capability — rotatable from the frame settings; optional
  sender PIN to stop link sharing abuse.
- Upload limits: max size (e.g. 25 MB), allowed types (jpeg/png/heic/mp4),
  rate-limit per pairing code.
- Blobs encrypted at rest on the relay; deleted promptly after pull.
- **Documented limitation:** on the free tier the relay can read photos in
  transit. E2E encryption (sender→frame, relay blind) is a future option.

## Frame side

- `frame/connect_client.py` — maintains the WSS control channel, pulls blobs,
  imports them. Opt-in: only started when `connect.enabled: true` in config
  (wired in `frame/server.py`; managed from `/remote` via `/api/connect`).
- Config block:
  ```yaml
  connect:
    enabled: false
    relay_url: "https://connect.neotactiq.ai"
    device_token: ""     # set on first register
    pairing_code: ""     # returned by relay
  ```
- Degrades gracefully: if the relay is unreachable the frame runs normally and
  retries with backoff (same pattern as the MQTT bridge).

## MVP scope vs. later

**MVP (this spec):** register, control channel, send one photo, pull + import,
magic-link sender page.

**Later (paid tiers):** durable encrypted cloud backup, household dashboard
(manage several frames), email-to-frame, push notifications, E2E encryption.

## Open questions

- Blob transport: relay buffer+pull (chosen, simple) vs. streaming relay
  (lower storage, more complex). MVP = buffer+pull.
- Identity for senders: anonymous magic link (MVP) vs. light accounts for
  history/attribution.
- Where does a received photo live — its own album that the user can prune, or
  merged into the slideshow directly? MVP: own `received` source.
