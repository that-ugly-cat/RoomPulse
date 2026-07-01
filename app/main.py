"""RoomPulse — FastAPI app.

Loop live: il presenter crea/usa un run, attiva una slide e ne cambia lo stato;
il pubblico entra col join_code (fisso sulla presentation → risolto al run attivo),
vede la slide attiva via polling, e vota. Aggregazione on-the-fly.

Dev run:  uv run uvicorn app.main:app --reload --port 8080
Seed:     uv run python seed.py
"""

import csv
import io
import json
import os

import segno
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path

from app import db, auth, locales, cluster as clustering
from app.aggregate import aggregate, SINGLE_VOTE_TYPES, MODERATED_TYPES

# dependency riusabile per le rotte presenter (alza 401 se non autenticato)
CurrentUser = Depends(auth.get_current_user)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="RoomPulse")


@app.on_event("startup")
def _startup():
    db.init_db()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ----------------------------------------------------------------------------
# Pagine
# ----------------------------------------------------------------------------
@app.get("/")
def audience_page():
    return FileResponse(STATIC_DIR / "audience.html")


@app.get("/present")
def presenter_page(session: str | None = Cookie(default=None)):
    if not auth.get_user_or_none(session):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "presenter.html")


@app.get("/edit")
def editor_page(session: str | None = Cookie(default=None)):
    if not auth.get_user_or_none(session):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "editor.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/guide")
def guide_page():
    return FileResponse(STATIC_DIR / "guide.html")


class LoginIn(BaseModel):
    email: str
    password: str


SIGNUP_CODE = os.environ.get("RP_SIGNUP_CODE")  # se settato, la registrazione lo richiede


@app.get("/api/auth-config")
def auth_config():
    return {"signup_code_required": bool(SIGNUP_CODE)}


class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""
    signup_code: str | None = None


@app.post("/api/register")
def register(body: RegisterIn, response: Response):
    if SIGNUP_CODE and (body.signup_code or "") != SIGNUP_CODE:
        raise HTTPException(403, "codice di registrazione non valido")
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "email non valida")
    if len(body.password) < 6:
        raise HTTPException(400, "password troppo corta (minimo 6 caratteri)")
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM user WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "email già registrata")
        uid = db.new_id()
        conn.execute(
            "INSERT INTO user (id, email, password_hash, name, is_active, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (uid, email, auth.hash_password(body.password),
             body.name.strip() or email.split("@")[0], db.now_iso()),
        )
    token = auth.create_token(uid)
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=auth.EXPIRE_DAYS * 86400
    )
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    with db.get_conn() as conn:
        u = conn.execute(
            "SELECT * FROM user WHERE email=? AND is_active=1", (body.email,)
        ).fetchone()
    if not u or not auth.verify_password(body.password, u["password_hash"]):
        raise HTTPException(401, "Credenziali errate")
    token = auth.create_token(u["id"])
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=auth.EXPIRE_DAYS * 86400
    )
    return {"ok": True, "name": u["name"]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = CurrentUser):
    with db.get_conn() as conn:
        row = conn.execute("SELECT api_key FROM user WHERE id=?", (user["id"],)).fetchone()
    key = row["api_key"] if row else None
    # non rivelo la chiave: solo se c'è e gli ultimi 4 char
    masked = ("…" + key[-4:]) if key else None
    return {"email": user["email"], "name": user["name"], "api_key_set": bool(key), "api_key_hint": masked}


class ApiKeyIn(BaseModel):
    api_key: str


@app.put("/api/me/api-key")
def set_api_key(body: ApiKeyIn, user: dict = CurrentUser):
    key = body.api_key.strip()
    with db.get_conn() as conn:
        conn.execute("UPDATE user SET api_key=? WHERE id=?", (key or None, user["id"]))
    return {"ok": True, "api_key_set": bool(key)}


@app.get("/api/i18n")
def i18n(lang: str = locales.DEFAULT):
    """Stringhe UI per la lingua richiesta (aperto, serve anche all'audience)."""
    chosen = lang if lang in locales.SUPPORTED else locales.DEFAULT
    return {"lang": chosen, "supported": list(locales.SUPPORTED), "t": locales.get_t(chosen)}


@app.get("/qr/{code}.svg")
def qr_svg(code: str, request: Request):
    base = str(request.base_url).rstrip("/")
    url = f"{base}/?c={code}"
    buff = io.BytesIO()
    segno.make(url).save(buff, kind="svg", scale=6, border=2)
    return Response(content=buff.getvalue(), media_type="image/svg+xml")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _slide_dict(row) -> dict:
    return {
        "id": row["id"],
        "ord": row["ord"],
        "type": row["type"],
        "question": row["question"],
        "config": json.loads(row["config"]),
        "pair_id": row["pair_id"],
    }


def _run_slide_state(conn, run_id: str, slide_id: str) -> str:
    row = conn.execute(
        "SELECT state FROM run_slide WHERE run_id=? AND slide_id=?",
        (run_id, slide_id),
    ).fetchone()
    return row["state"] if row else "pending"


def _merged_mc_options(conn, run_id: str, slide) -> list:
    """Opzioni base (config) + quelle aggiunte dai partecipanti in questo run."""
    config = json.loads(slide["config"])
    options = [dict(o) for o in config.get("options", [])]
    extra = conn.execute(
        "SELECT id, label FROM mc_option WHERE run_id=? AND slide_id=? ORDER BY created_at",
        (run_id, slide["id"]),
    ).fetchall()
    options += [{"id": r["id"], "label": r["label"], "added": True} for r in extra]
    return options


def _mc_results(conn, run_id: str, slide) -> dict:
    """mc: conta choice (single) e choices[] (multiple), su opzioni base + aggiunte."""
    config = json.loads(slide["config"])
    options = _merged_mc_options(conn, run_id, slide)
    rows = conn.execute(
        "SELECT payload FROM response WHERE run_id=? AND slide_id=? AND status='visible'",
        (run_id, slide["id"]),
    ).fetchall()
    counts: dict = {}
    n = 0
    for r in rows:
        p = json.loads(r["payload"])
        n += 1
        picks = p["choices"] if isinstance(p.get("choices"), list) else (
            [p["choice"]] if p.get("choice") is not None else []
        )
        for c in picks:
            counts[c] = counts.get(c, 0) + 1
    return {
        "type": "mc",
        "n": n,
        "multi": bool(config.get("multi")),
        "quiz": bool(config.get("quiz")),
        "correct": config.get("correct", []),
        "options": [
            {"id": o["id"], "label": o["label"], "count": counts.get(o["id"], 0),
             "added": o.get("added", False)}
            for o in options
        ],
    }


def _results(conn, run_id: str, slide) -> dict:
    if slide["type"] == "qa":
        return _qa_results(conn, run_id, slide["id"])
    if slide["type"] == "mc":
        return _mc_results(conn, run_id, slide)
    if slide["type"] == "argpoll":
        has = conn.execute(
            "SELECT 1 FROM cluster WHERE run_id=? AND slide_id=? LIMIT 1",
            (run_id, slide["id"]),
        ).fetchone()
        if has:
            return _argpoll_clustered(conn, run_id, slide["id"])
    if slide["type"] == "opentext":
        has = conn.execute(
            "SELECT 1 FROM cluster WHERE run_id=? AND slide_id=? AND kind='theme' LIMIT 1",
            (run_id, slide["id"]),
        ).fetchone()
        if has:
            return _opentext_clustered(conn, run_id, slide["id"])
    rows = conn.execute(
        "SELECT id, payload, status, created_at FROM response "
        "WHERE run_id=? AND slide_id=? AND status='visible' ORDER BY created_at",
        (run_id, slide["id"]),
    ).fetchall()
    return aggregate(slide["type"], json.loads(slide["config"]), rows)


def _argpoll_clustered(conn, run_id: str, slide_id: str) -> dict:
    """Vista clusterizzata: claim cluster annidati con tag dell'argomento + matrice claim×arg."""
    claim_cl = conn.execute(
        "SELECT id, label FROM cluster WHERE run_id=? AND slide_id=? AND kind='claim' ORDER BY ord",
        (run_id, slide_id),
    ).fetchall()
    arg_cl = conn.execute(
        "SELECT id, label FROM cluster WHERE run_id=? AND slide_id=? AND kind='arg' ORDER BY ord",
        (run_id, slide_id),
    ).fetchall()
    arg_label = {a["id"]: a["label"] for a in arg_cl}
    rows = conn.execute(
        "SELECT payload, claim_cluster_id, arg_cluster_id FROM response "
        "WHERE run_id=? AND slide_id=? AND status='visible'",
        (run_id, slide_id),
    ).fetchall()
    by_claim: dict = {c["id"]: [] for c in claim_cl}
    matrix: dict = {(c["id"], a["id"]): 0 for c in claim_cl for a in arg_cl}
    n = 0
    for r in rows:
        p = json.loads(r["payload"])
        n += 1
        cc, ac = r["claim_cluster_id"], r["arg_cluster_id"]
        if cc in by_claim:
            by_claim[cc].append({
                "claim": p.get("claim", ""),
                "justification": p.get("justification", ""),
                "arg_label": arg_label.get(ac, ""),
            })
        if (cc, ac) in matrix:
            matrix[(cc, ac)] += 1
    claims_out = sorted(
        [{"id": c["id"], "label": c["label"], "count": len(by_claim[c["id"]]),
          "items": by_claim[c["id"]]} for c in claim_cl],
        key=lambda x: -x["count"],
    )
    return {
        "type": "argpoll",
        "n": n,
        "clustered": True,
        "claim_clusters": claims_out,
        "arg_clusters": [{"id": a["id"], "label": a["label"]} for a in arg_cl],
        "matrix": [[matrix[(c["id"], a["id"])] for a in arg_cl] for c in claim_cl],
        "matrix_rows": [c["label"] for c in claim_cl],
        "matrix_cols": [a["label"] for a in arg_cl],
    }


def _opentext_clustered(conn, run_id: str, slide_id: str) -> dict:
    """Open text clusterizzato: cluster tematici (un asse) con le risposte annidate."""
    clusters = conn.execute(
        "SELECT id, label FROM cluster WHERE run_id=? AND slide_id=? AND kind='theme' ORDER BY ord",
        (run_id, slide_id),
    ).fetchall()
    rows = conn.execute(
        "SELECT payload, cluster_id FROM response "
        "WHERE run_id=? AND slide_id=? AND status='visible'",
        (run_id, slide_id),
    ).fetchall()
    by_cluster: dict = {c["id"]: [] for c in clusters}
    n = 0
    for r in rows:
        n += 1
        if r["cluster_id"] in by_cluster:
            by_cluster[r["cluster_id"]].append(json.loads(r["payload"]).get("text", ""))
    out = sorted(
        [{"id": c["id"], "label": c["label"], "count": len(by_cluster[c["id"]]),
          "items": by_cluster[c["id"]]} for c in clusters],
        key=lambda x: -x["count"],
    )
    return {"type": "opentext", "n": n, "clustered": True, "clusters": out}


def _materialize_text_clusters(conn, run_id: str, slide_id: str, result: dict) -> None:
    """Materializza il clustering a un asse (open text): cluster kind='theme' + response.cluster_id."""
    now = db.now_iso()
    conn.execute("DELETE FROM cluster WHERE run_id=? AND slide_id=?", (run_id, slide_id))
    conn.execute(
        "UPDATE response SET cluster_id=NULL WHERE run_id=? AND slide_id=?", (run_id, slide_id)
    )
    cmap: dict = {}
    for i, c in enumerate(result.get("clusters", [])):
        cid = db.new_id()
        conn.execute(
            "INSERT INTO cluster (id, run_id, slide_id, kind, label, ord, generated_at) "
            "VALUES (?,?,?,'theme',?,?,?)",
            (cid, run_id, slide_id, c.get("label", "—"), i, now),
        )
        cmap[c.get("id")] = cid
    resp_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM response WHERE run_id=? AND slide_id=? AND status='visible' "
            "ORDER BY created_at",
            (run_id, slide_id),
        ).fetchall()
    ]
    for a in result.get("assignments", []):
        nn = a.get("n")
        if not isinstance(nn, int) or nn < 1 or nn > len(resp_ids):
            continue
        conn.execute(
            "UPDATE response SET cluster_id=? WHERE id=?",
            (cmap.get(a.get("cluster")), resp_ids[nn - 1]),
        )


def _materialize_clusters(conn, run_id: str, slide_id: str, result: dict) -> None:
    """Salva l'esito del clustering LLM: cancella i precedenti, crea cluster, assegna le risposte."""
    now = db.now_iso()
    conn.execute("DELETE FROM cluster WHERE run_id=? AND slide_id=?", (run_id, slide_id))
    conn.execute(
        "UPDATE response SET claim_cluster_id=NULL, arg_cluster_id=NULL "
        "WHERE run_id=? AND slide_id=?",
        (run_id, slide_id),
    )
    claim_map: dict = {}
    for i, c in enumerate(result.get("claim_clusters", [])):
        cid = db.new_id()
        conn.execute(
            "INSERT INTO cluster (id, run_id, slide_id, kind, label, ord, generated_at) "
            "VALUES (?,?,?,'claim',?,?,?)",
            (cid, run_id, slide_id, c.get("label", "—"), i, now),
        )
        claim_map[c.get("id")] = cid
    arg_map: dict = {}
    for i, a in enumerate(result.get("arg_clusters", [])):
        aid = db.new_id()
        conn.execute(
            "INSERT INTO cluster (id, run_id, slide_id, kind, label, ord, generated_at) "
            "VALUES (?,?,?,'arg',?,?,?)",
            (aid, run_id, slide_id, a.get("label", "—"), i, now),
        )
        arg_map[a.get("id")] = aid
    resp_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM response WHERE run_id=? AND slide_id=? AND status='visible' "
            "ORDER BY created_at",
            (run_id, slide_id),
        ).fetchall()
    ]
    for a in result.get("assignments", []):
        n = a.get("n")
        if not isinstance(n, int) or n < 1 or n > len(resp_ids):
            continue
        conn.execute(
            "UPDATE response SET claim_cluster_id=?, arg_cluster_id=? WHERE id=?",
            (claim_map.get(a.get("claim")), arg_map.get(a.get("arg")), resp_ids[n - 1]),
        )


def _qa_results(conn, run_id: str, slide_id: str) -> dict:
    """qa ha bisogno del DB per i conteggi voti → non passa per aggregate()."""
    rows = conn.execute(
        "SELECT r.id, r.payload, "
        "  (SELECT COUNT(*) FROM qa_vote v WHERE v.response_id=r.id) AS votes "
        "FROM response r WHERE r.run_id=? AND r.slide_id=? AND r.status='visible' "
        "ORDER BY votes DESC, r.created_at ASC",
        (run_id, slide_id),
    ).fetchall()
    items = [
        {"id": r["id"], "text": json.loads(r["payload"]).get("text", ""), "votes": r["votes"]}
        for r in rows
    ]
    return {"type": "qa", "n": len(items), "items": items}


def _moderation(conn, run_id: str, slide_id: str) -> list:
    rows = conn.execute(
        "SELECT id, payload, status, created_at FROM response "
        "WHERE run_id=? AND slide_id=? ORDER BY created_at DESC",
        (run_id, slide_id),
    ).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        text = p.get("text") or p.get("claim") or ""
        out.append(
            {
                "id": r["id"],
                "text": text,
                "justification": p.get("justification"),
                "status": r["status"],
            }
        )
    return out


# ----------------------------------------------------------------------------
# API — presenter
# ----------------------------------------------------------------------------
class PresentationIn(BaseModel):
    title: str
    owner: str = "spit"


def _check_owner(conn, pid: str, user: dict):
    """Alza 404/403 se la presentation non esiste o non è dell'utente."""
    row = conn.execute("SELECT owner FROM presentation WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "presentation not found")
    if row["owner"] != user["id"]:
        raise HTTPException(403, "non autorizzato")


def _pid_of_run(conn, rid: str) -> str:
    r = conn.execute("SELECT presentation_id FROM run WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "run not found")
    return r["presentation_id"]


def _pid_of_slide(conn, sid: str) -> str:
    r = conn.execute("SELECT presentation_id FROM slide WHERE id=?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "slide not found")
    return r["presentation_id"]


@app.get("/api/presentations")
def list_presentations(user: dict = CurrentUser):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, p.join_code, "
            "  (SELECT COUNT(*) FROM slide s WHERE s.presentation_id=p.id) AS n_slides "
            "FROM presentation p WHERE p.owner=? ORDER BY p.created_at DESC",
            (user["id"],),
        ).fetchall()
        return [dict(r) for r in rows]


class RenameIn(BaseModel):
    title: str


@app.patch("/api/presentations/{pid}")
def rename_presentation(pid: str, body: RenameIn, user: dict = CurrentUser):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "titolo vuoto")
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        conn.execute("UPDATE presentation SET title=? WHERE id=?", (title, pid))
        return {"ok": True, "title": title}


@app.post("/api/presentations")
def create_presentation(body: PresentationIn, user: dict = CurrentUser):
    with db.get_conn() as conn:
        pid = db.new_id()
        code = db.new_join_code(conn)
        conn.execute(
            "INSERT INTO presentation (id, title, owner, join_code, created_at) "
            "VALUES (?,?,?,?,?)",
            (pid, body.title, user["id"], code, db.now_iso()),
        )
        return {"id": pid, "title": body.title, "join_code": code}


class SlideIn(BaseModel):
    type: str
    question: str
    config: dict = {}
    pair_id: str | None = None   # pre/post: questa slide è la POST della slide indicata (PRE)


@app.post("/api/presentations/{pid}/slides")
def add_slide(pid: str, body: SlideIn, user: dict = CurrentUser):
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        if body.pair_id:
            tgt = conn.execute(
                "SELECT type FROM slide WHERE id=? AND presentation_id=?",
                (body.pair_id, pid),
            ).fetchone()
            if not tgt:
                raise HTTPException(400, "slide pre/post non valida")
            if body.type not in ("scale", "mc", "quadrant") or tgt["type"] != body.type:
                raise HTTPException(
                    400, "pre/post consentito solo tra slide dello stesso tipo (scale/mc/quadrant)"
                )
        ord_row = conn.execute(
            "SELECT COALESCE(MAX(ord), 0) + 1 AS n FROM slide WHERE presentation_id=?",
            (pid,),
        ).fetchone()
        sid = db.new_id()
        conn.execute(
            "INSERT INTO slide (id, presentation_id, ord, type, question, config, pair_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, pid, ord_row["n"], body.type, body.question,
             json.dumps(body.config), body.pair_id),
        )
        return {"id": sid, "ord": ord_row["n"]}


class RunIn(BaseModel):
    label: str | None = None


@app.post("/api/presentations/{pid}/runs")
def start_run(pid: str, body: RunIn, user: dict = CurrentUser):
    """Crea un nuovo run e lo imposta come run corrente della presentation."""
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        rid = db.new_id()
        conn.execute(
            "INSERT INTO run (id, presentation_id, label, started_at) VALUES (?,?,?,?)",
            (rid, pid, body.label, db.now_iso()),
        )
        conn.execute(
            "UPDATE presentation SET active_run_id=? WHERE id=?", (rid, pid)
        )
        return {"run_id": rid}


class ActivateIn(BaseModel):
    slide_id: str


@app.post("/api/runs/{rid}/activate")
def activate_slide(rid: str, body: ActivateIn, user: dict = CurrentUser):
    """Rende attiva una slide nel run e la apre al voto."""
    with db.get_conn() as conn:
        _check_owner(conn, _pid_of_run(conn, rid), user)
        run = conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone()
        if not run:
            raise HTTPException(404, "run not found")
        slide = conn.execute(
            "SELECT 1 FROM slide WHERE id=? AND presentation_id=?",
            (body.slide_id, run["presentation_id"]),
        ).fetchone()
        if not slide:
            raise HTTPException(404, "slide not in this presentation")
        conn.execute("UPDATE run SET active_slide_id=? WHERE id=?", (body.slide_id, rid))
        conn.execute(
            "INSERT INTO run_slide (run_id, slide_id, state) VALUES (?,?, 'open') "
            "ON CONFLICT(run_id, slide_id) DO UPDATE SET state='open'",
            (rid, body.slide_id),
        )
        return {"active_slide_id": body.slide_id, "state": "open"}


class StateIn(BaseModel):
    slide_id: str
    state: str  # open | closed | revealed


@app.post("/api/runs/{rid}/state")
def set_state(rid: str, body: StateIn, user: dict = CurrentUser):
    if body.state not in ("open", "closed", "revealed"):
        raise HTTPException(400, "invalid state")
    with db.get_conn() as conn:
        _check_owner(conn, _pid_of_run(conn, rid), user)
        conn.execute(
            "INSERT INTO run_slide (run_id, slide_id, state) VALUES (?,?,?) "
            "ON CONFLICT(run_id, slide_id) DO UPDATE SET state=excluded.state",
            (rid, body.slide_id, body.state),
        )
        return {"slide_id": body.slide_id, "state": body.state}


@app.get("/api/presentations/{pid}")
def presenter_view(pid: str, user: dict = CurrentUser):
    """Vista completa per il presenter: deck + slide + run corrente + risultati attivi."""
    with db.get_conn() as conn:
        pres = conn.execute("SELECT * FROM presentation WHERE id=?", (pid,)).fetchone()
        if not pres:
            raise HTTPException(404, "presentation not found")
        if pres["owner"] != user["id"]:
            raise HTTPException(403, "non autorizzato")
        slides = conn.execute(
            "SELECT * FROM slide WHERE presentation_id=? ORDER BY ord", (pid,)
        ).fetchall()
        out = {
            "id": pres["id"],
            "title": pres["title"],
            "join_code": pres["join_code"],
            "active_run_id": pres["active_run_id"],
            "slides": [_slide_dict(s) for s in slides],
            "run": None,
        }
        rid = pres["active_run_id"]
        if rid:
            run = conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone()
            states = {
                r["slide_id"]: r["state"]
                for r in conn.execute(
                    "SELECT slide_id, state FROM run_slide WHERE run_id=?", (rid,)
                ).fetchall()
            }
            active = run["active_slide_id"]
            results = None
            moderation = None
            pair = None
            aslide = conn.execute("SELECT * FROM slide WHERE id=?", (active,)).fetchone() if active else None
            if aslide is None:
                active = None  # puntatore penzolante (slide cancellata) → nessuna attiva
            if active:
                results = _results(conn, rid, aslide)
                if aslide["type"] in MODERATED_TYPES:
                    moderation = _moderation(conn, rid, active)
                if aslide["pair_id"]:
                    pslide = conn.execute(
                        "SELECT * FROM slide WHERE id=?", (aslide["pair_id"],)
                    ).fetchone()
                    if pslide:
                        pair = {
                            "slide": _slide_dict(pslide),
                            "results": _results(conn, rid, pslide),
                        }
            out["run"] = {
                "id": rid,
                "label": run["label"],
                "active_slide_id": active,
                "states": states,
                "results": results,
                "moderation": moderation,
                "pair": pair,
            }
        return out


class SlideStatusIn(BaseModel):
    status: str  # visible | hidden | flagged


@app.post("/api/responses/{response_id}/status")
def set_response_status(response_id: str, body: SlideStatusIn, user: dict = CurrentUser):
    if body.status not in ("visible", "hidden", "flagged"):
        raise HTTPException(400, "invalid status")
    with db.get_conn() as conn:
        r = conn.execute("SELECT run_id FROM response WHERE id=?", (response_id,)).fetchone()
        if not r:
            raise HTTPException(404, "response not found")
        _check_owner(conn, _pid_of_run(conn, r["run_id"]), user)
        conn.execute("UPDATE response SET status=? WHERE id=?", (body.status, response_id))
        return {"id": response_id, "status": body.status}


@app.delete("/api/slides/{slide_id}")
def delete_slide(slide_id: str, user: dict = CurrentUser):
    with db.get_conn() as conn:
        _check_owner(conn, _pid_of_slide(conn, slide_id), user)
        # azzera i run che avevano questa slide come attiva (evita puntatori penzolanti)
        conn.execute("UPDATE run SET active_slide_id=NULL WHERE active_slide_id=?", (slide_id,))
        conn.execute(
            "DELETE FROM qa_vote WHERE response_id IN (SELECT id FROM response WHERE slide_id=?)",
            (slide_id,),
        )
        conn.execute("DELETE FROM mc_option WHERE slide_id=?", (slide_id,))
        conn.execute("DELETE FROM cluster WHERE slide_id=?", (slide_id,))
        conn.execute("DELETE FROM response WHERE slide_id=?", (slide_id,))
        conn.execute("DELETE FROM run_slide WHERE slide_id=?", (slide_id,))
        # scollega eventuali coppie pre/post che puntavano a questa slide
        conn.execute("UPDATE slide SET pair_id=NULL WHERE pair_id=?", (slide_id,))
        conn.execute("DELETE FROM slide WHERE id=?", (slide_id,))
        return {"ok": True}


@app.delete("/api/presentations/{pid}")
def delete_presentation(pid: str, user: dict = CurrentUser):
    """Cancella una deck e TUTTI i dati associati (slide, run, risposte, voti)."""
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        slide_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM slide WHERE presentation_id=?", (pid,)
            ).fetchall()
        ]
        run_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM run WHERE presentation_id=?", (pid,)
            ).fetchall()
        ]
        for rid in run_ids:
            conn.execute(
                "DELETE FROM qa_vote WHERE response_id IN (SELECT id FROM response WHERE run_id=?)",
                (rid,),
            )
            conn.execute("DELETE FROM response WHERE run_id=?", (rid,))
            conn.execute("DELETE FROM run_slide WHERE run_id=?", (rid,))
            conn.execute("DELETE FROM mc_option WHERE run_id=?", (rid,))
            conn.execute("DELETE FROM cluster WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM run WHERE presentation_id=?", (pid,))
        for sid in slide_ids:
            conn.execute("DELETE FROM slide WHERE id=?", (sid,))
        conn.execute("DELETE FROM presentation WHERE id=?", (pid,))
        return {"ok": True, "deleted_slides": len(slide_ids), "deleted_runs": len(run_ids)}


class ReorderIn(BaseModel):
    slide_ids: list[str]


@app.post("/api/presentations/{pid}/reorder")
def reorder_slides(pid: str, body: ReorderIn, user: dict = CurrentUser):
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        existing = {
            r["id"] for r in conn.execute(
                "SELECT id FROM slide WHERE presentation_id=?", (pid,)
            ).fetchall()
        }
        if set(body.slide_ids) != existing:
            raise HTTPException(400, "lista slide incompleta")
        # due fasi per non violare UNIQUE(presentation_id, ord)
        for sid in body.slide_ids:
            conn.execute("UPDATE slide SET ord=ord+10000 WHERE id=?", (sid,))
        for i, sid in enumerate(body.slide_ids, start=1):
            conn.execute("UPDATE slide SET ord=? WHERE id=?", (i, sid))
        return {"ok": True}


# ── Export / Import deck (solo config, non i dati delle run) ──────────────────
@app.get("/api/presentations/{pid}/export")
def export_deck(pid: str, user: dict = CurrentUser):
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        p = conn.execute("SELECT * FROM presentation WHERE id=?", (pid,)).fetchone()
        slides = conn.execute(
            "SELECT * FROM slide WHERE presentation_id=? ORDER BY ord", (pid,)
        ).fetchall()
        return {
            "roompulse_deck": 1,
            "title": p["title"],
            "slides": [
                {
                    "ref": s["id"],
                    "type": s["type"],
                    "question": s["question"],
                    "config": json.loads(s["config"]),
                    "pair_ref": s["pair_id"],
                }
                for s in slides
            ],
        }


class ImportSlide(BaseModel):
    ref: str | None = None
    type: str
    question: str
    config: dict = {}
    pair_ref: str | None = None


class ImportDeck(BaseModel):
    title: str
    slides: list[ImportSlide]


@app.post("/api/presentations/import")
def import_deck(body: ImportDeck, user: dict = CurrentUser):
    with db.get_conn() as conn:
        pid = db.new_id()
        code = db.new_join_code(conn)
        conn.execute(
            "INSERT INTO presentation (id, title, owner, join_code, created_at) VALUES (?,?,?,?,?)",
            (pid, body.title, user["id"], code, db.now_iso()),
        )
        new_ids: list[str] = []
        refmap: dict[str, str] = {}
        for i, s in enumerate(body.slides, start=1):
            sid = db.new_id()
            conn.execute(
                "INSERT INTO slide (id, presentation_id, ord, type, question, config) "
                "VALUES (?,?,?,?,?,?)",
                (sid, pid, i, s.type, s.question, json.dumps(s.config)),
            )
            new_ids.append(sid)
            if s.ref:
                refmap[s.ref] = sid
        for i, s in enumerate(body.slides):
            if s.pair_ref and s.pair_ref in refmap:
                conn.execute(
                    "UPDATE slide SET pair_id=? WHERE id=?", (refmap[s.pair_ref], new_ids[i])
                )
        return {"id": pid, "title": body.title, "join_code": code, "n_slides": len(body.slides)}


def _fmt_answer(stype: str, p: dict, optmap: dict) -> str:
    """Formatta il payload di una risposta in una stringa leggibile per il CSV."""
    if stype == "mc":
        picks = p["choices"] if isinstance(p.get("choices"), list) else (
            [p["choice"]] if p.get("choice") is not None else [])
        return "; ".join(optmap.get(c, c) for c in picks)
    if stype == "scale":
        return str(p.get("value", ""))
    if stype == "quadrant":
        return f'{p.get("x", "")},{p.get("y", "")}'
    if stype == "ranking":
        return " > ".join(optmap.get(i, i) for i in p.get("order", []))
    if stype == "points":
        return "; ".join(f"{optmap.get(k, k)}:{v}" for k, v in (p.get("alloc") or {}).items())
    if stype in ("wordcloud", "opentext", "qa"):
        return p.get("text", "")
    if stype == "groups":
        return p.get("group_name", "")
    if stype == "donut":
        return str(p.get("score", ""))
    if stype == "argpoll":
        return ""  # claim/justification vanno nelle loro colonne
    return json.dumps(p, ensure_ascii=False)


@app.get("/api/presentations/{pid}/data.csv")
def export_data(pid: str, user: dict = CurrentUser):
    """CSV grezzo delle risposte del run attivo: una riga per risposta, con cluster se presenti."""
    with db.get_conn() as conn:
        _check_owner(conn, pid, user)
        pres = conn.execute("SELECT * FROM presentation WHERE id=?", (pid,)).fetchone()
        rid = pres["active_run_id"]
        if not rid:
            raise HTTPException(400, "nessun run attivo da esportare")
        clabels = {
            c["id"]: c["label"]
            for c in conn.execute(
                "SELECT id, label FROM cluster WHERE run_id=?", (rid,)
            ).fetchall()
        }
        slides = conn.execute(
            "SELECT * FROM slide WHERE presentation_id=? ORDER BY ord", (pid,)
        ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["slide_ord", "slide_type", "question", "participant", "created_at",
                    "status", "claim", "justification", "answer", "cluster", "arg_cluster"])
        for s in slides:
            cfg = json.loads(s["config"])
            optmap: dict = {}
            if s["type"] == "mc":
                for o in cfg.get("options", []):
                    optmap[o["id"]] = o["label"]
                for mo in conn.execute(
                    "SELECT id, label FROM mc_option WHERE run_id=? AND slide_id=?", (rid, s["id"])
                ).fetchall():
                    optmap[mo["id"]] = mo["label"]
            elif s["type"] == "ranking":
                for o in cfg.get("items", []):
                    optmap[o["id"]] = o["label"]
            elif s["type"] == "points":
                for o in cfg.get("options", []):
                    optmap[o["id"]] = o["label"]
            rows = conn.execute(
                "SELECT participant_token, payload, status, created_at, "
                "claim_cluster_id, arg_cluster_id, cluster_id FROM response "
                "WHERE run_id=? AND slide_id=? ORDER BY created_at",
                (rid, s["id"]),
            ).fetchall()
            for r in rows:
                p = json.loads(r["payload"])
                claim = p.get("claim", "") if s["type"] == "argpoll" else ""
                just = p.get("justification", "") if s["type"] == "argpoll" else ""
                primary = clabels.get(r["claim_cluster_id"]) or clabels.get(r["cluster_id"]) or ""
                argc = clabels.get(r["arg_cluster_id"]) or ""
                w.writerow([s["ord"], s["type"], s["question"], r["participant_token"],
                            r["created_at"], r["status"], claim, just,
                            _fmt_answer(s["type"], p, optmap), primary, argc])
        fname = "".join(ch if ch.isalnum() else "_" for ch in (pres["title"] or "deck")).lower()[:40]
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}_dati.csv"'},
        )


# ----------------------------------------------------------------------------
# API — audience (pubblico, via join_code)
# ----------------------------------------------------------------------------
def _resolve_run(conn, code: str):
    pres = conn.execute(
        "SELECT * FROM presentation WHERE join_code=?", (code,)
    ).fetchone()
    if not pres:
        raise HTTPException(404, "codice non valido")
    return pres


@app.get("/api/live/{code}")
def live(code: str):
    """Ciò che il client del pubblico richiede in polling."""
    with db.get_conn() as conn:
        pres = _resolve_run(conn, code)
        rid = pres["active_run_id"]
        if not rid:
            return {"status": "waiting", "title": pres["title"]}
        run = conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone()
        active = run["active_slide_id"]
        if not active:
            return {"status": "waiting", "title": pres["title"]}
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (active,)).fetchone()
        if slide is None:  # puntatore penzolante
            return {"status": "waiting", "title": pres["title"]}
        state = _run_slide_state(conn, rid, active)
        payload = {
            "status": "live",
            "run_id": rid,
            "state": state,
            "slide": _slide_dict(slide),
        }
        if slide["type"] == "qa":
            # qa: il pubblico vede e vota le domande altrui anche mentre è 'open'
            payload["feed"] = _qa_results(conn, rid, active)
        if slide["type"] == "donut":
            # leaderboard non segreta: sempre allegata (l'audience mostra top3 se open, tutto se closed)
            payload["results"] = _results(conn, rid, slide)
        if slide["type"] == "mc":
            # mc: opzioni base + quelle aggiunte dai partecipanti (allow_other)
            cfg = payload["slide"]["config"]
            cfg["options"] = _merged_mc_options(conn, rid, slide)
            cfg.pop("correct", None)  # mai svelare la risposta corretta durante il voto
        if state == "revealed":
            payload["results"] = _results(conn, rid, slide)
            if slide["pair_id"]:  # pre/post: l'audience vede lo stesso confronto del presenter
                pslide = conn.execute(
                    "SELECT * FROM slide WHERE id=?", (slide["pair_id"],)
                ).fetchone()
                if pslide:
                    payload["pair"] = {
                        "slide": _slide_dict(pslide),
                        "results": _results(conn, rid, pslide),
                    }
        return payload


class AddOptionIn(BaseModel):
    slide_id: str
    label: str


@app.post("/api/live/{code}/add-option")
def add_option(code: str, body: AddOptionIn):
    """Un partecipante aggiunge un'opzione mc (se allow_other) — visibile a tutti."""
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "etichetta vuota")
    with db.get_conn() as conn:
        pres = _resolve_run(conn, code)
        rid = pres["active_run_id"]
        if not rid:
            raise HTTPException(409, "nessun run attivo")
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (body.slide_id,)).fetchone()
        if not slide or slide["type"] != "mc":
            raise HTTPException(404, "slide non valida")
        if not json.loads(slide["config"]).get("allow_other"):
            raise HTTPException(403, "aggiunta opzioni non consentita")
        oid = "x" + db.new_id()[:8]
        conn.execute(
            "INSERT INTO mc_option (id, run_id, slide_id, label, created_at) VALUES (?,?,?,?,?)",
            (oid, rid, body.slide_id, label, db.now_iso()),
        )
        return {"id": oid, "label": label}


class UpvoteIn(BaseModel):
    response_id: str
    token: str


@app.post("/api/live/{code}/upvote")
def upvote(code: str, body: UpvoteIn):
    """Toggle dell'upvote di una domanda qa (un voto per token)."""
    with db.get_conn() as conn:
        pres = _resolve_run(conn, code)
        rid = pres["active_run_id"]
        if not rid:
            raise HTTPException(409, "nessun run attivo")
        row = conn.execute(
            "SELECT slide_id FROM response WHERE id=?", (body.response_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "domanda non trovata")
        if _run_slide_state(conn, rid, row["slide_id"]) != "open":
            raise HTTPException(409, "domande chiuse")
        exists = conn.execute(
            "SELECT 1 FROM qa_vote WHERE response_id=? AND token=?",
            (body.response_id, body.token),
        ).fetchone()
        if exists:
            conn.execute(
                "DELETE FROM qa_vote WHERE response_id=? AND token=?",
                (body.response_id, body.token),
            )
            return {"voted": False}
        conn.execute(
            "INSERT INTO qa_vote (run_id, response_id, token) VALUES (?,?,?)",
            (rid, body.response_id, body.token),
        )
        return {"voted": True}


class RespondIn(BaseModel):
    slide_id: str
    token: str
    payload: dict


@app.post("/api/live/{code}/respond")
def respond(code: str, body: RespondIn):
    with db.get_conn() as conn:
        pres = _resolve_run(conn, code)
        rid = pres["active_run_id"]
        if not rid:
            raise HTTPException(409, "nessun run attivo")
        run = conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone()
        if run["active_slide_id"] != body.slide_id:
            raise HTTPException(409, "la slide non è più attiva")
        if _run_slide_state(conn, rid, body.slide_id) != "open":
            raise HTTPException(409, "voto chiuso")
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (body.slide_id,)).fetchone()

        # quiz mc: risposta definitiva (non sovrascrivibile) + ritorno la correttezza
        if slide["type"] == "mc":
            cfg = json.loads(slide["config"])
            if cfg.get("quiz"):
                correct = set(cfg.get("correct", []))

                def _picks(p):
                    return (p.get("choices") if isinstance(p.get("choices"), list)
                            else ([p["choice"]] if p.get("choice") is not None else []))

                ex = conn.execute(
                    "SELECT payload FROM response WHERE run_id=? AND slide_id=? AND participant_token=?",
                    (rid, body.slide_id, body.token),
                ).fetchone()
                if ex:  # ha già risposto → bloccato, ritorno il suo esito originale
                    picks = _picks(json.loads(ex["payload"]))
                    return {"ok": True, "locked": True,
                            "quiz": {"correct": set(picks) == correct, "correct_ids": list(correct)}}
                conn.execute(
                    "INSERT INTO response (id, run_id, slide_id, participant_token, payload, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (db.new_id(), rid, body.slide_id, body.token, json.dumps(body.payload), db.now_iso()),
                )
                picks = _picks(body.payload)
                return {"ok": True, "quiz": {"correct": set(picks) == correct, "correct_ids": list(correct)}}

        # donut: best-score per token (tieni il massimo) + cap anti-assurdo
        if slide["type"] == "donut":
            raw = body.payload.get("score")
            if not isinstance(raw, (int, float)) or raw < 0 or raw > 100000:
                raise HTTPException(400, "punteggio non valido")
            score = int(raw)
            name = str(body.payload.get("name") or "").strip()[:40]
            ex = conn.execute(
                "SELECT id, payload FROM response WHERE run_id=? AND slide_id=? AND participant_token=?",
                (rid, body.slide_id, body.token),
            ).fetchone()
            if ex:
                prev = json.loads(ex["payload"])
                payload = {"score": max(score, int(prev.get("score", 0))), "name": name or prev.get("name", "—")}
                conn.execute("UPDATE response SET payload=? WHERE id=?", (json.dumps(payload), ex["id"]))
            else:
                payload = {"score": score, "name": name or "—"}
                conn.execute(
                    "INSERT INTO response (id, run_id, slide_id, participant_token, payload, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (db.new_id(), rid, body.slide_id, body.token, json.dumps(payload), db.now_iso()),
                )
            return {"ok": True, "best": payload["score"]}

        # voto singolo → upsert: rimuovo il voto precedente di questo token
        if slide["type"] in SINGLE_VOTE_TYPES:
            conn.execute(
                "DELETE FROM response WHERE run_id=? AND slide_id=? AND participant_token=?",
                (rid, body.slide_id, body.token),
            )
        conn.execute(
            "INSERT INTO response (id, run_id, slide_id, participant_token, payload, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (db.new_id(), rid, body.slide_id, body.token, json.dumps(body.payload), db.now_iso()),
        )
        return {"ok": True}


class AssignIn(BaseModel):
    slide_id: str
    token: str


@app.post("/api/live/{code}/assign")
def assign(code: str, body: AssignIn):
    """Randomizzatore: assegna il partecipante a un gruppo (bilanciato, stabile per token)."""
    with db.get_conn() as conn:
        pres = _resolve_run(conn, code)
        rid = pres["active_run_id"]
        if not rid:
            raise HTTPException(409, "nessun run attivo")
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (body.slide_id,)).fetchone()
        if not slide or slide["type"] != "groups":
            raise HTTPException(404, "slide non valida")
        # assegnazione esistente → stabile
        ex = conn.execute(
            "SELECT payload FROM response WHERE run_id=? AND slide_id=? AND participant_token=?",
            (rid, body.slide_id, body.token),
        ).fetchone()
        if ex:
            return json.loads(ex["payload"])
        if _run_slide_state(conn, rid, body.slide_id) == "closed":
            raise HTTPException(409, "assegnazioni chiuse")
        groups = json.loads(slide["config"]).get("groups", [])
        if not groups:
            raise HTTPException(400, "nessun gruppo definito")
        # bilanciamento: assegna al gruppo attualmente meno numeroso
        counts = {g["id"]: 0 for g in groups}
        for r in conn.execute(
            "SELECT payload FROM response WHERE run_id=? AND slide_id=?", (rid, body.slide_id)
        ).fetchall():
            gid = json.loads(r["payload"]).get("group_id")
            if gid in counts:
                counts[gid] += 1
        chosen = min(groups, key=lambda g: counts[g["id"]])
        payload = {"group_id": chosen["id"], "group_name": chosen["name"]}
        conn.execute(
            "INSERT INTO response (id, run_id, slide_id, participant_token, payload, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (db.new_id(), rid, body.slide_id, body.token, json.dumps(payload), db.now_iso()),
        )
        return payload


class ClusterIn(BaseModel):
    slide_id: str


@app.post("/api/runs/{rid}/cluster")
def cluster_run_slide(rid: str, body: ClusterIn, user: dict = CurrentUser):
    """Clusterizza (LLM) le risposte argpoll/opentext del run. Usa la chiave API dell'utente."""
    with db.get_conn() as conn:
        _check_owner(conn, _pid_of_run(conn, rid), user)
        row = conn.execute("SELECT api_key FROM user WHERE id=?", (user["id"],)).fetchone()
        key = row["api_key"] if row else None
        if not key:
            raise HTTPException(400, "API key non configurata")
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (body.slide_id,)).fetchone()
        if not slide or slide["type"] not in ("argpoll", "opentext"):
            raise HTTPException(404, "slide non valida")
        rows = conn.execute(
            "SELECT payload FROM response WHERE run_id=? AND slide_id=? AND status='visible' "
            "ORDER BY created_at",
            (rid, body.slide_id),
        ).fetchall()
        if len(rows) < 2:
            raise HTTPException(400, "servono almeno 2 risposte per clusterizzare")
        question = slide["question"]
        try:
            if slide["type"] == "argpoll":
                pairs = []
                for i, r in enumerate(rows, start=1):
                    p = json.loads(r["payload"])
                    pairs.append({"n": i, "claim": p.get("claim", ""), "justification": p.get("justification", "")})
                result = clustering.cluster_argpoll(key, question, pairs)
            else:  # opentext
                texts = []
                for i, r in enumerate(rows, start=1):
                    texts.append({"n": i, "text": json.loads(r["payload"]).get("text", "")})
                result = clustering.cluster_opentext(key, question, texts)
        except Exception as e:  # errore LLM / chiave / parsing
            raise HTTPException(502, f"clustering fallito: {e}")
        if slide["type"] == "argpoll":
            _materialize_clusters(conn, rid, body.slide_id, result)
            return _argpoll_clustered(conn, rid, body.slide_id)
        _materialize_text_clusters(conn, rid, body.slide_id, result)
        return _opentext_clustered(conn, rid, body.slide_id)


@app.get("/api/live/{code}/results")
def live_results(code: str):
    with db.get_conn() as conn:
        pres = _resolve_run(conn, code)
        rid = pres["active_run_id"]
        if not rid:
            return JSONResponse({"status": "waiting"})
        run = conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone()
        active = run["active_slide_id"]
        if not active:
            return JSONResponse({"status": "waiting"})
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (active,)).fetchone()
        if slide is None:
            return JSONResponse({"status": "waiting"})
        res = _results(conn, rid, slide)
        # quiz: non svelare la risposta corretta finché la slide non è rivelata
        if res.get("quiz") and _run_slide_state(conn, rid, active) != "revealed":
            res = {**res, "correct": []}
        return res
