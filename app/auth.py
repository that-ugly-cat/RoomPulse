"""Autenticazione del presenter — JWT in cookie httpOnly.

Mirroring di `tools/automap-v2/deploy/auth.py`, adattato a sqlite raw (niente SQLAlchemy).
- Token in cookie 'session', durata EXPIRE_DAYS, rinnovato a ogni login.
- Secret da env JWT_SECRET (default insicuro solo per dev → cambialo in produzione).
- `get_current_user`: dependency per le rotte API protette (alza 401).
- `get_user_or_none`: per le rotte HTML che fanno redirect a /login invece di 401.
"""

import hmac
import ipaddress
import logging
import os
import secrets
import sqlite3
from contextvars import ContextVar
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

# ── Provisioning anticipato ──────────────────────────────────────────────────
#
# Borant ID puo' dire in anticipo chi potra' entrare, cosi' il profilo esiste
# prima del primo accesso e si puo' preparare un corso la sera prima invece che
# durante la lezione. Non e' un requisito: senza questo, tutto continua a
# funzionare come prima, perche' il profilo nasce comunque al primo accesso.
#
# **Due lucchetti, e sono spenti tutt'e due di default.** Il segreto vale come
# credenziale, l'IP dice che la chiamata arriva dal gate sulla rete docker
# condivisa e non da internet. Manca uno dei due e la rotta non esiste: e' la
# stessa forma del §8, per la stessa ragione — un'app che crede a una
# credenziale senza guardare da dove arriva ha un lucchetto solo.
#
# Quello che questa rotta puo' fare e' volutamente stretto: **creare profili
# che non esistono**. Non aggiorna, non promuove, non disattiva. Un segreto
# rubato compra account vuoti, non il lavoro di qualcun altro.
PROVISION_SECRET = os.environ.get("PROVISION_SECRET", "").strip()
PROVISION_TRUSTED = _parse_trusted(os.environ.get("PROVISION_TRUSTED", ""))


def provisioning_enabled() -> bool:
    return bool(PROVISION_SECRET and PROVISION_TRUSTED)


def provision_caller_ok(request: Request, authorization: str | None) -> bool:
    """Chi chiama ha il segreto **e** arriva da dove deve."""
    if not provisioning_enabled():
        return False
    peer = request.client.host if request.client else None
    try:
        addr = ipaddress.ip_address(peer) if peer else None
    except ValueError:
        addr = None
    if addr is None or not any(addr in net for net in PROVISION_TRUSTED):
        log.warning("/internal/provision da %s, fuori da PROVISION_TRUSTED: rifiutato", peer)
        return False
    given = (authorization or "")
    if not given.lower().startswith("bearer "):
        return False
    # `compare_digest` e non `==`: il confronto di un segreto non deve durare
    # un tempo che dipende da quanti caratteri ha indovinato chi prova.
    return hmac.compare_digest(given[7:].strip(), PROVISION_SECRET)


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


def _role_from_hint(hint: str, chi: str) -> str:
    """Il ruolo di partenza che il gate suggerisce, o il default che non spende.

    L'hint del gate propone, e vengono onorati tutti e tre — non solo `free`.

    La regola del §18 dice di non provisionare mai da un header un ruolo che
    spende, e `full` e `admin` spendono: usano la chiave Anthropic centrale del
    server, senza tetto per utente da nessuna parte. La deroga e' deliberata e
    vale per la stessa ragione di Grant Radar: la regola nasce da un'app con la
    **registrazione aperta**, dove l'hint porta cio' che ha chiesto *chi bussa*.
    Su Borant ID la registrazione aperta e' spenta, e anche una richiesta
    d'accesso fa scegliere il ruolo all'amministratore al momento di approvare
    — quindi in questo header `full` o `admin` ci sono solo perche' un umano li
    ha digitati.

    Prima il vocabolario del gate ne dichiarava tre e il codice ne accettava
    uno: un menu che offre ruoli che il codice non guarda e' peggio di nessun
    menu, ed e' lo stesso difetto corretto su Grant Radar il 24/8/2026.

    Quello che il codice deve comunque e' **rumore**: un ruolo che spende,
    concesso per questa via, lo dice a voce alta. Un hint non riconosciuto e' un
    refuso, non un ruolo, e ricade sul default che non spende.
    """
    hint = (hint or "").strip().lower()
    if hint in RUOLI_NOTI:
        if hint in RUOLI_CHE_SPENDONO:
            log.warning(
                "%s creato come %r su suggerimento del gate. Quel ruolo usa la "
                "chiave Anthropic centrale, senza tetto per utente. Revocare "
                "da /admin se non era voluto.", chi, hint)
        return hint
    if hint:
        log.warning("hint %r non in %s, ricado su %r",
                    hint, sorted(RUOLI_NOTI), GATEWAY_DEFAULT_ROLE)
    return GATEWAY_DEFAULT_ROLE


def provision(conn, sub: str, email: str, name: str, hint: str) -> tuple[str, dict | None]:
    """Il profilo locale di chi il gate conosce come `sub`, creandolo se manca.

    Torna `(esito, profilo)`, dove esito e' uno di:

      already    c'e' gia' una riga legata a questo subject, e non si tocca
      created    la riga e' stata creata adesso
      conflict   un profilo locale ha gia' quell'indirizzo e nessun legame

    Una funzione sola perche' le strade sono due — il primo accesso dietro il
    gate, e l'annuncio anticipato che Borant ID manda a `/internal/provision`
    quando concede l'accesso — e un utente nuovo deve trovare la stessa cosa da
    tutt'e due. Due copie divergerebbero al primo effetto collaterale aggiunto
    a una sola delle due.

    Il `conflict` non e' un errore da aggirare: legare per email significa che
    un refuso nel pannello del gate fonde due account, e quel rischio non
    migliora perche' l'operazione avviene a orario d'ufficio invece che a
    runtime. Si risolve a mano con `map_borant.py --map email=subject`.
    """
    row = conn.execute(
        "SELECT id, email, name, role, is_active FROM user WHERE borant_sub=?",
        (sub,),
    ).fetchone()
    if row:
        return "already", dict(row)

    email = (email or "").strip().lower() or f"{sub}@borant.invalid"
    name = (name or "").strip() or email.split("@")[0]

    taken = conn.execute(
        "SELECT id FROM user WHERE lower(email)=? AND borant_sub IS NULL",
        (email,),
    ).fetchone()
    if taken:
        log.error("%s arriva come %s, ma un profilo locale ha gia' quell'indirizzo "
                  "e nessun legame. Lancia `python map_borant.py --map %s=%s` "
                  "invece di lasciare indovinare.", email, sub, email, sub)
        return "conflict", None

    role = _role_from_hint(hint, f"{email} ({sub})")
    # Una password locale che non conosce nessuno, invece di nessuna: serve
    # a tenere `AUTH_MODE=local` una via di ritorno funzionante. Chi e' stato
    # creato cosi' e poi torna indietro fa un reset, non trova una riga rotta.
    uid = db.new_id()
    try:
        conn.execute(
            "INSERT INTO user (id, email, password_hash, name, is_active, role, created_at, borant_sub) "
            "VALUES (?,?,?,?,1,?,?,?)",
            (uid, email, hash_password(secrets.token_urlsafe(32)), name, role,
             db.now_iso(), sub),
        )
    except sqlite3.IntegrityError:
        # Due richieste della stessa persona in parallelo — la pagina e la sua
        # XHR — passano tutt'e due dalla SELECT prima che l'altra abbia
        # inserito, e `ix_user_borant_sub` boccia la seconda. Con quattro
        # presenter non capita mai; con cento matricole che entrano allo stesso
        # minuto capita, e capita come un 500 all'inizio della lezione. Chi
        # perde la corsa rilegge la riga dell'altro, che e' la stessa persona.
        row = conn.execute(
            "SELECT id, email, name, role, is_active FROM user WHERE borant_sub=?",
            (sub,),
        ).fetchone()
        if row:
            return "already", dict(row)
        raise

    log.info("profilo nuovo per %s (%s), ruolo %s", email, sub, role)
    return "created", {"id": uid, "email": email, "name": name, "role": role,
                       "is_active": 1}


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
        esito, user = provision(
            conn, sub,
            request.headers.get("x-borant-email", ""),
            request.headers.get("x-borant-name", ""),
            request.headers.get("x-borant-hint", ""),
        )
    if user is None or not user.get("is_active", 1):
        return None
    return {k: user[k] for k in ("id", "email", "name", "role")}


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


# ── Superficie MCP: chi sta chiamando ────────────────────────────────────────
# Il middleware risolve la chiave e deposita qui il chiamante. `stateless_http`
# significa una richiesta per chiamata, quindi il contextvar vale per quella e basta.
_mcp_caller: ContextVar[dict | None] = ContextVar("mcp_caller", default=None)


def new_mcp_key() -> str:
    return "rp_" + secrets.token_urlsafe(32)


def check_mcp_key(key: str) -> dict | None:
    """L'utente proprietario di questa chiave, o None.

    Timbra `last_used_at`: una chiave che qualcuno sta ancora usando da qualche
    parte deve essere visibile come tale prima di revocarla."""
    if not key:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT k.id AS kid, u.id, u.email, u.name, u.role FROM mcp_key k "
            "JOIN user u ON u.id = k.user_id "
            "WHERE k.key=? AND k.active=1 AND u.is_active=1",
            (key,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE mcp_key SET last_used_at=? WHERE id=?", (db.now_iso(), row["kid"]))
    d = dict(row)
    d.pop("kid", None)
    return d


def set_mcp_caller(user: dict | None) -> None:
    _mcp_caller.set(user)


def mcp_caller() -> dict:
    """Il proprietario della chiave con cui e' arrivata questa chiamata.

    Alza invece di restituire None: un tool che gira senza chiamante non deve
    poter vedere nulla, e il middleware ha gia' rifiutato prima di arrivare qui."""
    user = _mcp_caller.get()
    if user is None:
        raise PermissionError("nessun chiamante autenticato")
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
