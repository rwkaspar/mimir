"""mimir Connect relay.

Lets a phone send a photo to a mimir frame that sits behind NAT, with no
port-forwarding on the user's side: the frame holds an outbound WebSocket
control channel to this relay; a sender uploads a photo via a magic link; the
relay notifies the frame, which pulls the blob and imports it locally.

See ../../docs/CONNECT.md for the design.

Storage:  SQLite (frames + blob metadata) + blobs on disk under RELAY_DATA_DIR.
Auth:     trust-on-first-use device token (hashed), short-lived HS256 JWTs.
Config:   env RELAY_JWT_SECRET (required in prod), RELAY_DATA_DIR (default ./data).

Run:  RELAY_JWT_SECRET=... uvicorn app:app --port 8800
"""

import hashlib
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import (FastAPI, HTTPException, UploadFile, File, Form, Header,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, Response

import jwt_min

logger = logging.getLogger("mimir.connect")
logging.basicConfig(level=logging.INFO)

BLOB_TTL_SECONDS = 24 * 3600
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/heic", "video/mp4"}
JWT_TTL = 12 * 3600
SEND_RATE_PER_MIN = 20  # per pairing code

DATA_DIR = Path(os.environ.get("RELAY_DATA_DIR", "data"))
BLOB_DIR = DATA_DIR / "blobs"
DB_PATH = DATA_DIR / "relay.db"

JWT_SECRET = os.environ.get("RELAY_JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_urlsafe(32)
    logger.warning("RELAY_JWT_SECRET not set - using an ephemeral secret; "
                   "tokens will not survive a restart. Set it in production.")

app = FastAPI(title="mimir Connect relay")

# Live WebSocket per pairing code (not persisted; rebuilt on reconnect).
_live_ws: Dict[str, WebSocket] = {}
# naive per-pairing-code send rate limiter: code -> [timestamps]
_send_hits: Dict[str, list] = {}


# --- storage ----------------------------------------------------------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_storage():
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS frames (
            pairing_code TEXT PRIMARY KEY,
            device_id    TEXT UNIQUE NOT NULL,
            token_hash   TEXT NOT NULL,
            sender_pin   TEXT,
            created_at   INTEGER NOT NULL,
            last_seen    INTEGER
        );
        CREATE TABLE IF NOT EXISTS blobs (
            id           TEXT PRIMARY KEY,
            pairing_code TEXT NOT NULL,
            content_type TEXT NOT NULL,
            caption      TEXT,
            created_at   INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL
        );
        """)


_init_storage()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _purge_expired():
    now = int(time.time())
    with _db() as c:
        rows = c.execute("SELECT id FROM blobs WHERE expires_at < ?", (now,)).fetchall()
        for r in rows:
            (BLOB_DIR / r["id"]).unlink(missing_ok=True)
            c.execute("DELETE FROM blobs WHERE id = ?", (r["id"],))


def _frame_by_code(code: str) -> Optional[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM frames WHERE pairing_code = ?", (code,)).fetchone()


def _issue_jwt(code: str, device_id: str) -> str:
    return jwt_min.encode({"sub": device_id, "code": code}, JWT_SECRET, JWT_TTL)


def _verify_jwt(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        return jwt_min.decode(authorization.split(" ", 1)[1], JWT_SECRET)
    except jwt_min.JWTError as e:
        raise HTTPException(401, f"invalid token: {e}")


# --- frame registration (trust-on-first-use) --------------------------------
@app.post("/v1/register")
async def register(device_id: str = Form(...), device_token: str = Form(...)):
    th = _hash(device_token)
    with _db() as c:
        row = c.execute("SELECT * FROM frames WHERE device_id = ?", (device_id,)).fetchone()
        if row:
            if not secrets.compare_digest(row["token_hash"], th):
                raise HTTPException(403, "device token mismatch")
            code = row["pairing_code"]
            c.execute("UPDATE frames SET last_seen = ? WHERE device_id = ?",
                      (int(time.time()), device_id))
        else:
            code = "MIMIR-" + secrets.token_hex(2).upper()
            c.execute("INSERT INTO frames(pairing_code, device_id, token_hash, "
                      "created_at, last_seen) VALUES (?,?,?,?,?)",
                      (code, device_id, th, int(time.time()), int(time.time())))
            logger.info("registered new frame %s -> %s", device_id, code)
    return {"pairing_code": code, "jwt": _issue_jwt(code, device_id),
            "jwt_ttl": JWT_TTL}


# --- frame control channel --------------------------------------------------
async def _deliver_backlog(ws: WebSocket, code: str):
    with _db() as c:
        rows = c.execute("SELECT id FROM blobs WHERE pairing_code = ? "
                         "ORDER BY created_at", (code,)).fetchall()
    for r in rows:
        await ws.send_text(f"photo:{r['id']}")


@app.websocket("/v1/agent")
async def agent(ws: WebSocket):
    await ws.accept()
    code = ws.query_params.get("pairing_code", "")
    token = ws.query_params.get("jwt", "")
    try:
        claims = jwt_min.decode(token, JWT_SECRET)
    except jwt_min.JWTError:
        await ws.close(code=4401)
        return
    if claims.get("code") != code or not _frame_by_code(code):
        await ws.close(code=4401)
        return

    _live_ws[code] = ws
    with _db() as c:
        c.execute("UPDATE frames SET last_seen = ? WHERE pairing_code = ?",
                  (int(time.time()), code))
    logger.info("frame %s control channel up", code)
    try:
        await _deliver_backlog(ws, code)
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
            elif msg.startswith("pulled:"):
                bid = msg.split(":", 1)[1]
                (BLOB_DIR / bid).unlink(missing_ok=True)
                with _db() as c:
                    c.execute("DELETE FROM blobs WHERE id = ? AND pairing_code = ?",
                              (bid, code))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("agent %s loop ended: %s", code, e)
    finally:
        if _live_ws.get(code) is ws:
            _live_ws.pop(code, None)


# --- sender uploads a photo -------------------------------------------------
def _rate_ok(code: str) -> bool:
    now = time.time()
    hits = [t for t in _send_hits.get(code, []) if now - t < 60]
    if len(hits) >= SEND_RATE_PER_MIN:
        _send_hits[code] = hits
        return False
    hits.append(now)
    _send_hits[code] = hits
    return True


@app.post("/v1/send")
async def send(pairing_code: str = Form(...), caption: str = Form(""),
               pin: str = Form(""), photo: UploadFile = File(...)):
    _purge_expired()
    frame = _frame_by_code(pairing_code)
    if not frame:
        raise HTTPException(404, "unknown pairing code")
    if frame["sender_pin"] and not secrets.compare_digest(frame["sender_pin"], pin):
        raise HTTPException(403, "wrong or missing PIN")
    if not _rate_ok(pairing_code):
        raise HTTPException(429, "too many sends; slow down")
    if photo.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "unsupported media type")
    data = await photo.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")

    bid = secrets.token_urlsafe(12)
    (BLOB_DIR / bid).write_bytes(data)
    now = int(time.time())
    with _db() as c:
        c.execute("INSERT INTO blobs(id, pairing_code, content_type, caption, "
                  "created_at, expires_at) VALUES (?,?,?,?,?,?)",
                  (bid, pairing_code, photo.content_type, caption, now,
                   now + BLOB_TTL_SECONDS))

    delivered = False
    ws = _live_ws.get(pairing_code)
    if ws is not None:
        try:
            await ws.send_text(f"photo:{bid}")
            delivered = True
        except Exception:
            pass  # frame will get it from backlog on reconnect
    return {"ok": True, "blob_id": bid, "delivered": delivered}


# --- frame pulls a blob -----------------------------------------------------
@app.get("/v1/blob/{blob_id}")
async def get_blob(blob_id: str, authorization: Optional[str] = Header(None)):
    claims = _verify_jwt(authorization)
    with _db() as c:
        row = c.execute("SELECT * FROM blobs WHERE id = ?", (blob_id,)).fetchone()
    if not row:
        raise HTTPException(404, "not found or expired")
    if row["pairing_code"] != claims.get("code"):
        raise HTTPException(403, "not your blob")
    path = BLOB_DIR / blob_id
    if not path.exists():
        raise HTTPException(404, "blob gone")
    return Response(content=path.read_bytes(), media_type=row["content_type"],
                    headers={"X-Mimir-Caption": row["caption"] or ""})


# --- magic-link sender page -------------------------------------------------
@app.get("/s/{code}", response_class=HTMLResponse)
async def sender_page(code: str):
    frame = _frame_by_code(code)
    if not frame:
        return HTMLResponse("<h1>Unknown frame link</h1>", status_code=404)
    pin_field = ('<p><input name=pin placeholder="PIN" style="width:100%"></p>'
                 if frame["sender_pin"] else '')
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Send a photo to mimir</title>
<body style="font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;background:#111;color:#eee">
<h2>Send a photo to this frame</h2>
<form method=post action="/v1/send" enctype="multipart/form-data">
  <input type=hidden name=pairing_code value="{code}">
  <p><input type=file name=photo accept="image/*,video/mp4" required></p>
  <p><input name=caption placeholder="Caption (optional)" style="width:100%"></p>
  {pin_field}
  <button type=submit style="padding:12px 20px">Send</button>
</form></body>""")


@app.get("/healthz")
async def healthz():
    _purge_expired()
    with _db() as c:
        frames = c.execute("SELECT COUNT(*) n FROM frames").fetchone()["n"]
        blobs = c.execute("SELECT COUNT(*) n FROM blobs").fetchone()["n"]
    return {"ok": True, "frames": frames, "pending_blobs": blobs,
            "online": len(_live_ws)}
