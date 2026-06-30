"""Autenticazione del presenter — JWT in cookie httpOnly.

Mirroring di `tools/automap-v2/deploy/auth.py`, adattato a sqlite raw (niente SQLAlchemy).
- Token in cookie 'session', durata EXPIRE_DAYS, rinnovato a ogni login.
- Secret da env JWT_SECRET (default insicuro solo per dev → cambialo in produzione).
- `get_current_user`: dependency per le rotte API protette (alza 401).
- `get_user_or_none`: per le rotte HTML che fanno redirect a /login invece di 401.
"""

import os
from datetime import datetime, timedelta

import bcrypt
from fastapi import Cookie, HTTPException, status
from jose import JWTError, jwt

from app import db

SECRET_KEY = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
ALGORITHM = "HS256"
EXPIRE_DAYS = 7


# ── Password (bcrypt diretto; limite hard di 72 byte) ────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


# ── JWT ──────────────────────────────────────────────────────────────────────
def create_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(days=EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return str(payload["sub"])
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessione non valida")


def _lookup(uid: str):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name FROM user WHERE id=? AND is_active=1", (uid,)
        ).fetchone()
    return dict(row) if row else None


# ── Dependencies ─────────────────────────────────────────────────────────────
def get_current_user(session: str | None = Cookie(default=None)) -> dict:
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non autenticato")
    user = _lookup(_decode_token(session))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Utente non trovato")
    return user


def get_user_or_none(session: str | None) -> dict | None:
    if not session:
        return None
    try:
        uid = _decode_token(session)
    except HTTPException:
        return None
    return _lookup(uid)
