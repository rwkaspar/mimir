"""Minimal HS256 JWT (encode/verify) using only the standard library.

Avoids a PyJWT dependency for the relay. Supports the subset we need:
HS256 signing, `exp` expiry, and tamper/expiry verification.
"""

import base64
import hashlib
import hmac
import json
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def encode(payload: dict, secret: str, ttl_seconds: int = 3600) -> str:
    header = {'alg': 'HS256', 'typ': 'JWT'}
    body = dict(payload)
    now = int(time.time())
    body.setdefault('iat', now)
    body['exp'] = now + ttl_seconds
    seg = _b64url(json.dumps(header, separators=(',', ':')).encode()) + '.' + \
        _b64url(json.dumps(body, separators=(',', ':')).encode())
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + '.' + _b64url(sig)


class JWTError(Exception):
    pass


def decode(token: str, secret: str) -> dict:
    try:
        seg, sig = token.rsplit('.', 1)
        expected = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            raise JWTError('bad signature')
        payload = json.loads(_b64url_decode(seg.split('.', 1)[1]))
    except JWTError:
        raise
    except Exception as e:
        raise JWTError(f'malformed token: {e}')
    if 'exp' in payload and int(payload['exp']) < int(time.time()):
        raise JWTError('expired')
    return payload
