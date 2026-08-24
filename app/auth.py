"""Autenticazione del presenter — JWT in cookie httpOnly.

Mirroring di `tools/automap-v2/deploy/auth.py`, adattato a sqlite raw (niente SQLAlchemy).
- Token in cookie 'session', durata EXPIRE_DAYS, rinnovato a ogni login.
- Secret da env JWT_SECRET (default insicuro solo per dev → cambialo in produzione).
- `get_current_user`: dependency per le rotte API protette (alza 401).
- `get_user_or_none`: per le rotte HTML che fanno redirect a /login invece di 401.
"""

import ipaddress
import logging
import os
import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import Cookie, HTTPException, Request, status
from jose import JWTError, jwt

from app import db

log = logging.getLogger("roompulse.auth")

SECRET_KEY = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
ALGORITHM = "HS256"
EXPIRE_DAYS = 7

# Due modi di riconoscere un presenter, e `local` e' il default di proposito:
# un'app che crede a un header d'identita' senza un gate davanti fa entrare
# chiunque spedisca quell'header. Il percorso `gateway` resta codice morto
# finche' qualcuno non lo accende apposta.
#
#   local     email + password sulla tabella user, come ha sempre funzionato
#   gateway   un gate SSO a monte garantisce per chi chiama, via X-Borant-*
#
# Il pubblico non c'entra: /api/live/* e la pagina d'ingresso restano anonime in
# entrambe le modalita', perche' chi partecipa non ha e non deve avere un account.
AUTH_MODE = os.environ.get("AUTH_MODE", "local").strip().lower()

# In `gateway` gli header d'identita' si credono solo se arrivano da qui — il
# reverse proxy, mai da internet. Sotto Docker e' il gateway di una rete bridge
# e NON 127.0.0.1: vedi DEPLOY.md per leggerlo da un container che gira.
TRUSTED_PROXY = os.environ.get("BORANT_TRUSTED_PROXY", "127.0.0.1")

# Il ruolo con cui nasce un profilo creato dal gate. `free` e non altro, e non e'
# una preferenza: `full` e `admin` clusterizzano con la chiave centrale del
# server, quindi un provisioning automatico verso quei ruoli aprirebbe un
# rubinetto sul conto di chi ospita. Salire di ruolo resta una decisione umana.
GATEWAY_DEFAULT_ROLE = "free"
# Il vocabolario che il gate dichiara in /admin/apps deve combaciare con questo,
# o il pannello offre ruoli che qui non arrivano da nessuna parte.
RUOLI_NOTI = {"free", "full", "admin"}
# `full` e `admin` clusterizzano con la chiave Anthropic del server: spendono.
RUOLI_CHE_SPENDONO = {"full", "admin"}


def _parse_trusted(raw: str) -> list:
    nets = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            log.warning("BORANT_TRUSTED_PROXY: ignoro %r, non e' un indirizzo o un CIDR", chunk)
    return nets


TRUSTED_PROXIES = _parse_trusted(TRUSTED_PROXY)


def gateway_mode() -> bool:
    return AUTH_MODE == "gateway"


def _from_trusted_proxy(request: Request) -> bool:
    peer = request.client.host if request.client else None
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def user_from_gateway(request: Request) -> dict | None:
    """Il presenter per cui il gate garantisce, o None.

    La ricerca e' per `borant_sub` e mai per email: legare per indirizzo a
    runtime farebbe fondere due account al primo errore di battitura nel
    pannello del gate. Chi arriva con un subject sconosciuto ottiene un profilo
    NUOVO, non quello di qualcun altro; a legare i profili esistenti ci pensa
    map_borant.py, che si legge prima di lanciarlo.
    """
    if not gateway_mode():
        return None
    sub = request.headers.get("x-borant-sub")
    if not sub:
        return None
    if not _from_trusted_proxy(request):
        log.warning("X-Borant-Sub da %s, fuori da BORANT_TRUSTED_PROXY (%s): ignorato",
                    request.client.host if request.client else "?", TRUSTED_PROXY)
        return None

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, role FROM user WHERE borant_sub=? AND is_active=1",
            (sub,),
        ).fetchone()
        if row:
            return dict(row)

        email = (request.headers.get("x-borant-email", "") or f"{sub}@borant.invalid").strip().lower()
        name = request.headers.get("x-borant-name", "") or email.split("@")[0]
        # L'hint del gate propone il ruolo di partenza, e da oggi vengono
        # onorati tutti e tre — non solo `free`.
        #
        # La regola del §18 dice di non provisionare mai da un header un ruolo
        # che spende, e `full` e `admin` spendono: usano la chiave Anthropic
        # centrale del server, senza tetto per utente da nessuna parte. La
        # deroga e' deliberata e vale per la stessa ragione di Grant Radar: la
        # regola nasce da un'app con la **registrazione aperta**, dove l'hint
        # porta cio' che ha chiesto *chi bussa*. Su Borant ID la registrazione
        # aperta e' spenta, e anche una richiesta d'accesso fa scegliere il
        # ruolo all'amministratore al momento di approvare — quindi in questo
        # header `full` o `admin` ci sono solo perche' un umano li ha digitati.
        #
        # Prima il vocabolario del gate ne dichiarava tre e il codice ne
        # accettava uno: un menu che offre ruoli che il codice non guarda e'
        # peggio di nessun menu, ed e' lo stesso difetto corretto su Grant
        # Radar il 24/8/2026.
        #
        # Quello che il codice deve comunque e' **rumore**: un ruolo che spende,
        # concesso per questa via, lo dice a voce alta. Un hint non riconosciuto
        # e' un refuso, non un ruolo, e ricade sul default che non spende.
        hint = (request.headers.get("x-borant-hint", "") or "").strip().lower()
        if hint in RUOLI_NOTI:
            role = hint
            if role in RUOLI_CHE_SPENDONO:
                log.warning(
                    "gateway: %s (%s) creato come %r su suggerimento del gate. "
                    "Quel ruolo usa la chiave Anthropic centrale, senza tetto "
                    "per utente. Revocare da /admin se non era voluto.",
                    email, sub, role)
        else:
            if hint:
                log.warning("gateway: hint %r non in %s, ricado su %r",
                            hint, sorted(RUOLI_NOTI), GATEWAY_DEFAULT_ROLE)
            role = GATEWAY_DEFAULT_ROLE
        # Una password locale che non conosce nessuno, invece di nessuna: serve
        # a tenere `AUTH_MODE=local` una via di ritorno funzionante. Chi e' stato
        # creato cosi' e poi torna indietro fa un reset, non trova una riga rotta.
        uid = db.new_id()
        conn.execute(
            "INSERT INTO user (id, email, password_hash, name, is_active, role, created_at, borant_sub) "
            "VALUES (?,?,?,?,1,?,?,?)",
            (uid, email, hash_password(secrets.token_urlsafe(32)), name, role,
             db.now_iso(), sub),
        )
        log.info("gateway: profilo nuovo per %s (%s), ruolo %s", email, sub, role)
        return {"id": uid, "email": email, "name": name, "role": role}


# ── Password (bcrypt diretto; limite hard di 72 byte) ────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


# ── JWT ──────────────────────────────────────────────────────────────────────
def create_token(user_id: str, token_version: int = 0) -> str:
    expire = datetime.utcnow() + timedelta(days=EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "v": int(token_version), "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def _decode_token(token: str) -> tuple[str, int]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        # i token emessi prima di questa colonna non hanno "v": valgono come 0, che e il
        # default in DB — cosi il deploy non butta fuori chi ha gia una sessione aperta
        return str(payload["sub"]), int(payload.get("v", 0))
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessione non valida")


def _lookup(uid: str, ver: int = 0):
    """Il token vale solo finche la sua versione combacia con quella in DB: cambiare
    password la incrementa, quindi le sessioni gia aperte cadono davvero."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, role, token_version FROM user WHERE id=? AND is_active=1",
            (uid,),
        ).fetchone()
    if not row or int(row["token_version"]) != int(ver):
        return None
    d = dict(row)
    d.pop("token_version", None)
    return d


# ── Dependencies ─────────────────────────────────────────────────────────────
def get_current_user(request: Request, session: str | None = Cookie(default=None)) -> dict:
    if gateway_mode():
        # L'header vince sul cookie, sempre: un cookie rimasto da prima non deve
        # sopravvivere a una sessione che il gate ha revocato.
        user = user_from_gateway(request)
        if user:
            return user
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non autenticato")
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non autenticato")
    user = _lookup(*_decode_token(session))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessione non piu valida")
    return user


def get_user_or_none(session: str | None, request: Request | None = None) -> dict | None:
    if gateway_mode():
        return user_from_gateway(request) if request is not None else None
    if not session:
        return None
    try:
        uid, ver = _decode_token(session)
    except HTTPException:
        return None
    return _lookup(uid, ver)
