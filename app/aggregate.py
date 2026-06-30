"""Aggregazione dei risultati, calcolata on-the-fly per-tipo.

Tipi v1: mc, scale, wordcloud, quadrant, ranking, points, opentext, argpoll.
(qa e pre/post arrivano nella spinta successiva.)

Le funzioni ricevono `config` (dict) e `rows` (sqlite Row con almeno `payload`;
i tipi feed usano anche `id`). Nota: il clustering LLM di argpoll (post-v1) NON passa
di qui — sarà materializzato e cachato (vedi DESIGN.md §10b).
"""

import json
from collections import Counter, defaultdict


def aggregate(slide_type: str, config: dict, rows) -> dict:
    fn = _DISPATCH.get(slide_type)
    if fn is None:
        return {"type": slide_type, "n": len(rows), "unsupported": True}
    return fn(config, rows)


def _payloads(rows):
    return [json.loads(r["payload"]) for r in rows]


# --- tipi a distribuzione -----------------------------------------------------
def _mc(config, rows):
    counts = Counter(p.get("choice") for p in _payloads(rows))
    options = config.get("options", [])
    return {
        "type": "mc",
        "n": len(rows),
        "options": [
            {"id": o["id"], "label": o["label"], "count": counts.get(o["id"], 0)}
            for o in options
        ],
    }


def _scale(config, rows):
    mn = int(config.get("min", 1))
    mx = int(config.get("max", 5))
    vals = [
        p["value"]
        for p in _payloads(rows)
        if isinstance(p.get("value"), (int, float)) and mn <= p["value"] <= mx
    ]
    hist = {v: 0 for v in range(mn, mx + 1)}
    for v in vals:
        hist[int(v)] += 1
    return {
        "type": "scale",
        "n": len(vals),
        "min": mn,
        "max": mx,
        "min_label": config.get("min_label", ""),
        "max_label": config.get("max_label", ""),
        "mean": (sum(vals) / len(vals)) if vals else None,
        "histogram": [{"value": v, "count": hist[v]} for v in range(mn, mx + 1)],
    }


def _wordcloud(config, rows):
    c = Counter()
    for p in _payloads(rows):
        t = (p.get("text") or "").strip().lower()
        if t:
            c[t] += 1
    return {
        "type": "wordcloud",
        "n": sum(c.values()),
        "terms": [{"term": k, "count": v} for k, v in c.most_common(50)],
    }


def _quadrant(config, rows):
    pts = []
    for p in _payloads(rows):
        if isinstance(p.get("x"), (int, float)) and isinstance(p.get("y"), (int, float)):
            pts.append({"x": p["x"], "y": p["y"]})
    return {
        "type": "quadrant",
        "n": len(pts),
        "points": pts,
        "labels": config.get("labels", {}),  # {x_left,x_right,y_top,y_bottom}
    }


def _ranking(config, rows):
    items = config.get("items", [])  # [{id,label}]
    pos_sum = defaultdict(int)
    pos_cnt = defaultdict(int)
    n = 0
    for p in _payloads(rows):
        order = p.get("order", [])
        if not order:
            continue
        n += 1
        for idx, iid in enumerate(order):
            pos_sum[iid] += idx + 1
            pos_cnt[iid] += 1
    res = []
    for it in items:
        c = pos_cnt.get(it["id"], 0)
        res.append(
            {
                "id": it["id"],
                "label": it["label"],
                "mean_rank": (pos_sum[it["id"]] / c) if c else None,
                "count": c,
            }
        )
    res.sort(key=lambda x: (x["mean_rank"] is None, x["mean_rank"] or 0))
    return {"type": "ranking", "n": n, "items": res}


def _points(config, rows):
    options = config.get("options", [])
    tot = defaultdict(int)
    n = 0
    for p in _payloads(rows):
        alloc = p.get("alloc", {})
        if not alloc:
            continue
        n += 1
        for k, v in alloc.items():
            tot[k] += v
    return {
        "type": "points",
        "n": n,
        "options": [
            {"id": o["id"], "label": o["label"], "total": tot.get(o["id"], 0)}
            for o in options
        ],
    }


# --- tipi feed (testuali, moderabili) ----------------------------------------
def _opentext(config, rows):
    items = [
        {"id": r["id"], "text": json.loads(r["payload"]).get("text", "")} for r in rows
    ]
    return {"type": "opentext", "n": len(items), "items": items}


def _groups(config, rows):
    """Randomizzatore: conta gli assegnati per gruppo."""
    groups = config.get("groups", [])
    counts: dict = {}
    for r in rows:
        gid = json.loads(r["payload"]).get("group_id")
        if gid:
            counts[gid] = counts.get(gid, 0) + 1
    return {
        "type": "groups",
        "n": sum(counts.values()),
        "groups": [
            {"id": g["id"], "name": g["name"], "count": counts.get(g["id"], 0)}
            for g in groups
        ],
    }


def _argpoll(config, rows):
    items = []
    for r in rows:
        p = json.loads(r["payload"])
        items.append(
            {
                "id": r["id"],
                "claim": p.get("claim", ""),
                "justification": p.get("justification", ""),
            }
        )
    return {"type": "argpoll", "n": len(items), "items": items}


_DISPATCH = {
    "mc": _mc,
    "scale": _scale,
    "wordcloud": _wordcloud,
    "quadrant": _quadrant,
    "ranking": _ranking,
    "points": _points,
    "opentext": _opentext,
    "argpoll": _argpoll,
    "groups": _groups,
}

# Voto singolo → upsert per (run, slide, token). Gli altri ammettono più invii.
SINGLE_VOTE_TYPES = {"mc", "scale", "quadrant", "ranking", "points"}

# Tipi testuali che passano per la coda di moderazione presenter-side.
MODERATED_TYPES = {"opentext", "argpoll", "qa"}
