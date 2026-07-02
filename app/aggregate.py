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


def _donut(config, rows):
    """Endless-runner: leaderboard + istogramma dei punteggi. Un record per token (best-score).
    Non moderato; l'upsert best-score sta in main.respond."""
    entries = []
    for p in _payloads(rows):
        s = p.get("score")
        if isinstance(s, (int, float)):
            entries.append({"name": (str(p.get("name") or "").strip()[:40]) or "—", "score": int(s)})
    entries.sort(key=lambda e: -e["score"])
    scores = sorted(e["score"] for e in entries)
    n = len(scores)
    mx = scores[-1] if scores else 0
    if n:
        mid = n // 2
        median = scores[mid] if n % 2 else (scores[mid - 1] + scores[mid]) // 2
    else:
        median = 0
    histogram = []
    if n:
        nb = 10
        width = max(1, mx // nb + 1)  # ~10 bucket da 0 al massimo
        counts = [0] * nb
        for s in scores:
            counts[min(nb - 1, s // width)] += 1
        histogram = [
            {"lo": i * width, "hi": (i + 1) * width - 1, "count": counts[i]} for i in range(nb)
        ]
    return {
        "type": "donut",
        "n": n,
        "leaderboard": entries[:10],
        "max": mx,
        "median": median,
        "histogram": histogram,
    }


def _connect(config, rows):
    """Mapping: conteggio per coppia (left,right) → matrice left×right."""
    left = config.get("left", [])
    right = config.get("right", [])
    li = {o["id"]: i for i, o in enumerate(left)}
    ri = {o["id"]: i for i, o in enumerate(right)}
    matrix = [[0] * len(right) for _ in left]
    n = 0
    for p in _payloads(rows):
        links = p.get("links", [])
        if links:
            n += 1
        seen = set()
        for pair in links:
            if isinstance(pair, list) and len(pair) == 2:
                l, r = pair
                if l in li and r in ri and (l, r) not in seen:
                    seen.add((l, r))
                    matrix[li[l]][ri[r]] += 1
    mx = max((c for row in matrix for c in row), default=0)
    return {
        "type": "connect",
        "n": n,
        "left": [{"id": o["id"], "label": o["label"]} for o in left],
        "right": [{"id": o["id"], "label": o["label"]} for o in right],
        "matrix": matrix,
        "max": mx,
        "cardinality": config.get("cardinality", "one-to-many"),
        "direction": config.get("direction", "ltr"),
    }


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
    "connect": _connect,
    "donut": _donut,
}

# Voto singolo → upsert per (run, slide, token). Gli altri ammettono più invii.
SINGLE_VOTE_TYPES = {"mc", "scale", "quadrant", "ranking", "points", "connect"}

# Tipi testuali che passano per la coda di moderazione presenter-side.
MODERATED_TYPES = {"opentext", "argpoll", "qa"}
