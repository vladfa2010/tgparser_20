#!/usr/bin/env python3
"""
TG Parser v2 — Auth.

Borrowed from pulse-backend security model (AUTH.md):
  - password stored as a strong hash only (PBKDF2-HMAC-SHA256, 600k iterations,
    stdlib — no heavy native deps in the 512 MB Render container);
  - JWT signed with JWT_SECRET, 7-day expiry, httpOnly + SameSite=Lax cookie
    (safer than pulse's Bearer-in-localStorage: JS can't read the token, and
    cross-site POSTs don't carry the cookie, which closes CSRF on mutations);
  - session version in DB: changing the password bumps admin_session_version,
    so every previously issued token dies instantly (pulse checks is_blocked
    from DB on each request — same idea, one admin user);
  - login rate limit: 15 attempts / 15 min per IP (pulse: authLimiter).
"""
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from db import Settings, async_session, get_setting, set_setting

JWT_SECRET = os.getenv("JWT_SECRET", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
COOKIE_NAME = "tg_admin"
TOKEN_TTL_DAYS = 7

_PBKDF2_ITERATIONS = 600_000

router = APIRouter()

# ─── Password hashing (stdlib PBKDF2) ────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ─── JWT ─────────────────────────────────────────────────────
def _issue_token(username: str, session_version: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + timedelta(days=TOKEN_TTL_DAYS),
         "sv": session_version},
        JWT_SECRET, algorithm="HS256",
    )


def _decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ─── Current user ────────────────────────────────────────────
async def get_current_user(request: Request) -> str | None:
    """Returns username if the request carries a valid admin session, else None."""
    if not JWT_SECRET:
        return None
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = _decode_token(token)
    if not payload:
        return None
    async with async_session() as session:
        sv = await get_setting(session, "admin_session_version", "1")
    if str(payload.get("sv")) != sv:
        return None
    return payload.get("sub")


async def require_admin(request: Request) -> str:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


# ─── Rate limit: 15 login attempts / 15 min per IP ───────────
_attempts: dict[str, deque] = defaultdict(deque)
_WINDOW_SEC = 15 * 60
_MAX_ATTEMPTS = 15


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > _WINDOW_SEC:
        q.popleft()
    if len(q) >= _MAX_ATTEMPTS:
        return True
    q.append(now)
    return False


# ─── First-run bootstrap ─────────────────────────────────────
async def ensure_admin():
    """Create the admin account on first launch. Prints a one-time generated
    password if ADMIN_PASSWORD env is not set."""
    async with async_session() as session:
        stored = await get_setting(session, "admin_password_hash", "")
        if stored:
            return
        password = os.getenv("ADMIN_PASSWORD", "")
        if not password:
            password = secrets.token_urlsafe(12)
            print("=" * 60)
            print(f"Initial admin password (shown once): {password}")
            print("Log in at /login and change it via «Сменить пароль».")
            print("=" * 60)
        await set_setting(session, "admin_username", ADMIN_USERNAME)
        await set_setting(session, "admin_password_hash", hash_password(password))
        await set_setting(session, "admin_session_version", "1")
        await session.commit()


# ─── Schemas ─────────────────────────────────────────────────
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordBody(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=8, max_length=200)


# ─── Routes ──────────────────────────────────────────────────
@router.post("/api/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте через 15 минут."}, status_code=429)

    async with async_session() as session:
        username = await get_setting(session, "admin_username", ADMIN_USERNAME)
        stored_hash = await get_setting(session, "admin_password_hash", "")
        sv = await get_setting(session, "admin_session_version", "1")

    ok = hmac.compare_digest(body.username.encode(), username.encode()) and verify_password(body.password, stored_hash)
    if not ok or not stored_hash:
        return JSONResponse({"error": "Неверный логин или пароль"}, status_code=401)

    token = _issue_token(username, sv)
    response.set_cookie(
        COOKIE_NAME, token, max_age=TOKEN_TTL_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
        secure=(request.url.scheme == "https"),
    )
    return {"ok": True}


@router.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/api/auth/change-password")
async def change_password(body: ChangePasswordBody, user: str = Depends(require_admin)):
    async with async_session() as session:
        stored_hash = await get_setting(session, "admin_password_hash", "")
        if not verify_password(body.current, stored_hash):
            return JSONResponse({"error": "Текущий пароль неверен"}, status_code=400)
        await set_setting(session, "admin_password_hash", hash_password(body.new))
        # Инвалидируем все ранее выданные токены
        sv = int(await get_setting(session, "admin_session_version", "1")) + 1
        await set_setting(session, "admin_session_version", str(sv))
        await session.commit()
    return {"ok": True}


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — TG Parser</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:32px;width:320px}
h1{margin:0 0 20px;font-size:20px;text-align:center}
input{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #475569;
border-radius:8px;color:#e2e8f0;padding:10px 12px;margin-bottom:12px;font-size:14px}
button{width:100%;background:#2563eb;border:none;border-radius:8px;color:#fff;
padding:10px;font-size:15px;cursor:pointer}
button:hover{background:#1d4ed8}
.err{color:#f87171;font-size:13px;min-height:18px;text-align:center;margin-bottom:8px}
</style></head><body>
<div class="card">
<h1>TG Parser — вход</h1>
<div class="err" id="err"></div>
<form id="f">
<input id="u" placeholder="Логин" autocomplete="username" required>
<input id="p" type="password" placeholder="Пароль" autocomplete="current-password" required>
<button type="submit">Войти</button>
</form>
</div>
<script>
document.getElementById('f').addEventListener('submit', async function(e){
  e.preventDefault();
  var r = await fetch('/api/auth/login', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username:document.getElementById('u').value,
                          password:document.getElementById('p').value})});
  if (r.ok) { location.href = '/admin'; }
  else { var d = await r.json().catch(function(){return {};});
    document.getElementById('err').textContent = d.error || ('Ошибка '+r.status); }
});
</script>
</body></html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await get_current_user(request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin", status_code=302)
    return HTMLResponse(content=LOGIN_HTML)
