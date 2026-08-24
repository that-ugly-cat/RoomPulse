"""
The model-facing surface of RoomPulse.

Two things happen outside the room and both of them happen in a conversation:
**writing** a deck before the lecture, and **reading** what the room answered
after it. The web editor is a good place to fix a slide and a bad place to
compose fifteen of them from a discussion that already produced the questions;
the results page is a good place to project a chart and a bad place to reason
about what the answers mean. So this surface covers those two ends and
deliberately leaves out the middle.

**What it will not do: drive the live room.** No start_run, no activate, no
open/close/reveal, no purge. Those decisions are scenic — they belong to the
person standing in front of the audience, watching faces, with the presenter
view already open on the screen. A model that closes a vote while a third of
the room is still typing has not helped anybody.

Access. Every call runs as the human who owns the API key, and every deck
lookup goes through the same _check_owner() the web app uses, so this surface
reaches exactly what its owner reaches, no more. A deck belonging to someone
else reports "not found" rather than "forbidden", so the model cannot enumerate
what it cannot see.

Errors come back as {"error": ...} rather than raised: a tool that throws hands
the model a stack trace to hallucinate around, while a sentence it can read
lets it correct course.
"""
import functools
import json

from mcp.server.mcpserver import MCPServer

from app import auth, db

mcp = MCPServer(
    name="roompulse",
    instructions=(
        "Live polling for lectures and keynotes: decks of questions, runs of a "
        "deck in front of a room, and what the room answered. Start with "
        "list_decks. Reads are free; before writing anything — a new deck, a "
        "slide, a reordering — confirm with the user. Call slide_types before "
        "composing a deck: each question type takes its own config, and a slide "
        "with a malformed config is accepted by the API and then breaks in "
        "front of the audience. This surface cannot open, close or reveal a "
        "vote: that is the presenter's, at the presenter's screen."
    ),
)


# `main` imports this module to mount it, so importing it back at module level
# would close a circle. It is imported per call instead, which costs a dict
# lookup and keeps one implementation of every rule.
def _rp():
    from app import main
    return main


def _fail(msg: str) -> dict:
    return {"error": msg}


def _guard(fn):
    """HTTPException -> {"error": ...}, and the ownership 403 flattened to "not
    found": the model must not learn that a deck it cannot see exists."""
    from fastapi import HTTPException

    @functools.wraps(fn)   # lo schema del tool nasce dalla firma: va conservata
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except HTTPException as e:
            if e.status_code == 403:
                return _fail("deck not found")
            return _fail(str(e.detail))
        except PermissionError as e:
            return _fail(str(e))
    return wrapped


def _deck(conn, deck_id: str):
    """The caller's deck, or None. Ownership is checked here, once."""
    row = conn.execute("SELECT * FROM presentation WHERE id=?", (deck_id,)).fetchone()
    if not row or row["owner"] != auth.mcp_caller()["id"]:
        return None
    return row


def _slide_brief(s, has_responses: bool = False) -> dict:
    return {
        "id": s["id"],
        "ord": s["ord"],
        "type": s["type"],
        "question": s["question"],
        "config": json.loads(s["config"]),
        "pair_id": s["pair_id"],
        "presenter_notes": s["presenter_notes"],
        # una slide con risposte non e' piu' modificabile nella struttura: dirlo
        # qui evita al modello di proporre una modifica che il server rifiutera'
        "structurally_locked": has_responses,
    }


# ── Reading ──────────────────────────────────────────────────────────────────
@mcp.tool()
@_guard
def list_decks() -> dict:
    """The caller's decks: join code, how many slides, whether a run is live."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, p.join_code, p.active_run_id, p.created_at, "
            "  (SELECT COUNT(*) FROM slide s WHERE s.presentation_id=p.id) AS slides, "
            "  (SELECT COUNT(*) FROM run r WHERE r.presentation_id=p.id) AS runs "
            "FROM presentation p WHERE p.owner=? ORDER BY p.created_at DESC",
            (auth.mcp_caller()["id"],),
        ).fetchall()
    return {"decks": [{"id": r["id"], "title": r["title"], "join_code": r["join_code"],
                       "slides": r["slides"], "runs": r["runs"],
                       "live": bool(r["active_run_id"]), "created": r["created_at"]}
                      for r in rows]}


@mcp.tool()
@_guard
def get_deck(deck_id: str) -> dict:
    """A deck in full: every slide with its type, question, config and notes."""
    with db.get_conn() as conn:
        p = _deck(conn, deck_id)
        if not p:
            return _fail("deck not found")
        slides = conn.execute(
            "SELECT * FROM slide WHERE presentation_id=? ORDER BY ord", (deck_id,)
        ).fetchall()
        answered = {r["slide_id"] for r in conn.execute(
            "SELECT DISTINCT slide_id FROM response WHERE run_id IN "
            "(SELECT id FROM run WHERE presentation_id=?)", (deck_id,)).fetchall()}
    return {"id": p["id"], "title": p["title"], "join_code": p["join_code"],
            "live": bool(p["active_run_id"]),
            "slides": [_slide_brief(s, s["id"] in answered) for s in slides]}


@mcp.tool()
@_guard
def list_runs(deck_id: str) -> dict:
    """The runs of a deck — one per time it was played in front of a room."""
    with db.get_conn() as conn:
        p = _deck(conn, deck_id)
        if not p:
            return _fail("deck not found")
        rows = conn.execute(
            "SELECT r.id, r.label, r.started_at, r.ended_at, "
            "  (SELECT COUNT(*) FROM response x WHERE x.run_id=r.id) AS responses, "
            "  (SELECT COUNT(DISTINCT x.participant_token) FROM response x WHERE x.run_id=r.id) AS people "
            "FROM run r WHERE r.presentation_id=? ORDER BY r.started_at DESC",
            (deck_id,),
        ).fetchall()
    return {"runs": [{"id": r["id"], "label": r["label"], "started": r["started_at"],
                      "ended": r["ended_at"], "responses": r["responses"],
                      "people": r["people"], "active": r["id"] == p["active_run_id"]}
                     for r in rows]}


@mcp.tool()
@_guard
def get_results(deck_id: str, run_id: str | None = None,
                slide_id: str | None = None) -> dict:
    """What a room answered, aggregated per slide. Defaults to the live run.

    `people` is the number of distinct participants on that slide and is not
    `n`: on a multiple-answer question one person ticks several boxes, and on a
    type that accepts repeated submissions one person leaves several answers.
    Use `people` as the denominator whenever you compute a share."""
    rp = _rp()
    with db.get_conn() as conn:
        p = _deck(conn, deck_id)
        if not p:
            return _fail("deck not found")
        rid = run_id or p["active_run_id"]
        if not rid:
            return _fail("this deck has no run yet: nothing has been answered")
        if not conn.execute("SELECT 1 FROM run WHERE id=? AND presentation_id=?",
                            (rid, deck_id)).fetchone():
            return _fail("run not found in this deck")
        q = "SELECT * FROM slide WHERE presentation_id=?" + (" AND id=?" if slide_id else "")
        args = (deck_id, slide_id) if slide_id else (deck_id,)
        slides = conn.execute(q + " ORDER BY ord", args).fetchall()
        if not slides:
            return _fail("slide not found in this deck")
        states = {r["slide_id"]: r["state"] for r in conn.execute(
            "SELECT slide_id, state FROM run_slide WHERE run_id=?", (rid,)).fetchall()}
        out = []
        for s in slides:
            out.append({
                "slide_id": s["id"], "ord": s["ord"], "type": s["type"],
                "question": s["question"],
                "state": states.get(s["id"], "pending"),
                "people": rp._voter_count(conn, rid, s["id"]),
                "results": rp._results(conn, rid, s),
            })
    return {"deck_id": deck_id, "run_id": rid, "slides": out}


@mcp.tool()
@_guard
def get_room(deck_id: str) -> dict:
    """The pulse of the room right now: which slide is live, how many people are
    connected, how many have answered it, and the results so far.

    `present` counts devices polling in the last twenty seconds, not people: two
    tabs of one browser count once, two browsers count twice. It is an honest
    order of magnitude, not a register."""
    rp = _rp()
    with db.get_conn() as conn:
        p = _deck(conn, deck_id)
        if not p:
            return _fail("deck not found")
        rid = p["active_run_id"]
        if not rid:
            return {"deck_id": deck_id, "title": p["title"], "join_code": p["join_code"],
                    "live": False, "detail": "no run is open on this deck"}
        run = conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone()
        active = run["active_slide_id"]
        slide = conn.execute("SELECT * FROM slide WHERE id=?", (active,)).fetchone() if active else None
        body = {"deck_id": deck_id, "title": p["title"], "join_code": p["join_code"],
                "live": True, "run_id": rid,
                "present": rp._present_count(conn, rid)}
        if slide is None:
            body["detail"] = "a run is open but no slide is active"
            return body
        body.update({
            "slide": {"id": slide["id"], "ord": slide["ord"], "type": slide["type"],
                      "question": slide["question"]},
            "state": rp._run_slide_state(conn, rid, active),
            "voters": rp._voter_count(conn, rid, active),
            "results": rp._results(conn, rid, slide),
        })
    return body


# Ciò che il modello deve sapere PRIMA di comporre: il tipo da solo non basta,
# perché ogni tipo ha la sua config e una config storta passa l'API e si rompe
# davanti all'aula.
SLIDE_TYPES = {
    "mc": {"what": "multiple choice, one or many answers; can be a quiz",
           "config": {"options": "[{id, label}] — required",
                      "multi": "bool: allow several answers",
                      "allow_other": "bool: participants may add options (not with quiz)",
                      "quiz": "bool: reveal right/wrong after answering",
                      "correct": "[option id] — with quiz"},
           "pairable": True},
    "scale": {"what": "Likert-style scale, histogram plus mean",
              "config": {"min": "int (default 1)", "max": "int (default 5)",
                         "min_label": "str", "max_label": "str"},
              "pairable": True},
    "quadrant": {"what": "two axes, a cloud of points",
                 "config": {"labels": "{x_left, x_right, y_top, y_bottom}"},
                 "pairable": True},
    "ranking": {"what": "drag items into order, mean rank per item",
                "config": {"items": "[{id, label}] — required"}},
    "points": {"what": "allocate a budget of points across options",
               "config": {"options": "[{id, label}] — required", "total": "int budget"}},
    "wordcloud": {"what": "short free text, aggregated by term",
                  "config": {"single": "bool: one submission per person"}},
    "opentext": {"what": "free text, moderated queue, LLM-clusterable",
                 "config": {"single": "bool: one submission per person"}},
    "qa": {"what": "audience questions with upvotes; open/closed, never revealed",
           "config": {}},
    "argpoll": {"what": "paired claim + justification, LLM-clusterable on both axes",
                "config": {"claim_label": "str", "justification_label": "str",
                           "single": "bool"}},
    "argstep": {"what": "the argument one move at a time; three slides, one chain",
                "config": {"fields": "prefix of [claim, justification, objection]",
                           "carry_from": "slide id of the previous step",
                           "carry_editable": "bool", "single": "bool"}},
    "groups": {"what": "randomiser: assigns each participant to a named group",
               "config": {"groups": "[{id, name}] — required"}},
    "connect": {"what": "map items on the left onto items on the right",
                "config": {"left": "[{id, label}]", "right": "[{id, label}]",
                           "cardinality": "one-to-one | one-to-many",
                           "direction": "ltr | rtl"}},
    "donut": {"what": "endless-runner energiser; leaderboard, not a question",
              "config": {}},
    "moonshot": {"what": "collaborative energiser: the room flies one rocket",
                 "config": {"min_players": "int (default 3)"}},
}


@mcp.tool()
def slide_types(type: str | None = None) -> dict:
    """The question types and the config each one takes. Read this before
    composing a deck. `pairable` means the type can carry a pre/post pair."""
    if type:
        spec = SLIDE_TYPES.get(type)
        return {type: spec} if spec else _fail(
            f"unknown type; known types: {', '.join(sorted(SLIDE_TYPES))}")
    return {"types": SLIDE_TYPES,
            "note": ("pre/post pairs only between two slides of the same pairable "
                     "type; in create_deck use `ref`/`pair_ref` to tie them, since "
                     "the ids do not exist yet.")}


# ── Writing ──────────────────────────────────────────────────────────────────
@mcp.tool()
@_guard
def create_deck(title: str, slides: list[dict]) -> dict:
    """Create a new deck from a list of slides. Confirm with the user first.

    Each slide: {type, question, config, presenter_notes?, ref?, pair_ref?}.
    `ref` is any string you choose; a slide that is the POST of a pre/post pair
    names the PRE's `ref` in its `pair_ref`. Returns the join code the room will
    type. This never touches an existing deck."""
    rp = _rp()
    if not slides:
        return _fail("a deck needs at least one slide")
    bad = [s.get("type") for s in slides if s.get("type") not in SLIDE_TYPES]
    if bad:
        return _fail(f"unknown slide type(s): {', '.join(str(b) for b in bad)}. "
                     "Call slide_types for the catalogue.")
    body = rp.ImportDeck(title=title, slides=[rp.ImportSlide(**s) for s in slides])
    out = rp.import_deck(body, user=auth.mcp_caller())
    with db.get_conn() as conn:
        p = conn.execute("SELECT join_code FROM presentation WHERE id=?",
                         (out["id"],)).fetchone()
    return {"deck_id": out["id"], "join_code": p["join_code"],
            "slides": out["n_slides"],
            "next": "open /edit to review it, /present?p=<deck_id> to run it"}


@mcp.tool()
@_guard
def add_slide(deck_id: str, type: str, question: str, config: dict | None = None,
              presenter_notes: str = "", pair_id: str | None = None) -> dict:
    """Append a slide to an existing deck. Confirm with the user first.

    `pair_id` makes this slide the POST of an existing PRE slide, which is only
    allowed between two slides of the same pairable type."""
    rp = _rp()
    if type not in SLIDE_TYPES:
        return _fail("unknown type; call slide_types for the catalogue")
    out = rp.add_slide(deck_id, rp.SlideIn(
        type=type, question=question, config=config or {},
        pair_id=pair_id, presenter_notes=presenter_notes), user=auth.mcp_caller())
    return {"slide_id": out["id"], "ord": out["ord"]}


@mcp.tool()
@_guard
def update_slide(slide_id: str, question: str | None = None,
                 config: dict | None = None,
                 presenter_notes: str | None = None) -> dict:
    """Change a slide. Confirm with the user first.

    Presenter notes can always be edited. Question and config only while the
    slide has no responses yet: once a room has answered it, changing what it
    asked would silently rewrite the past, and the server refuses."""
    rp = _rp()
    caller = auth.mcp_caller()
    structural = question is not None or config is not None
    if not structural and presenter_notes is None:
        return _fail("nothing to change")
    out = rp.edit_slide(slide_id, rp.SlideEdit(
        edit_structure=structural, question=question, config=config,
        presenter_notes=presenter_notes), user=caller)
    return {"ok": True, "scope": out["scope"]}


@mcp.tool()
@_guard
def reorder_slides(deck_id: str, slide_ids: list[str]) -> dict:
    """Set the order of a deck's slides. Confirm with the user first.

    The list must contain every slide of the deck exactly once — a partial list
    is refused rather than guessed at."""
    rp = _rp()
    rp.reorder_slides(deck_id, rp.ReorderIn(slide_ids=slide_ids), user=auth.mcp_caller())
    return {"ok": True, "order": slide_ids}
