"""Semina una deck demo e avvia un run, così l'app è subito dimostrabile.

    uv run python seed.py

Stampa l'URL del presenter e il join_code per il pubblico.
"""

import json

from app import db, auth


def main():
    db.init_db()
    with db.get_conn() as conn:
        # utente di default per la demo (cambia password in produzione!)
        row = conn.execute("SELECT id FROM user WHERE email='spit@local'").fetchone()
        if row:
            owner = row["id"]
        else:
            owner = db.new_id()
            conn.execute(
                "INSERT INTO user (id, email, password_hash, name, is_active, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (owner, "spit@local", auth.hash_password("roompulse"), "Spit", db.now_iso()),
            )
        pid = db.new_id()
        code = db.new_join_code(conn)
        conn.execute(
            "INSERT INTO presentation (id, title, owner, join_code, created_at) VALUES (?,?,?,?,?)",
            (pid, "RoomPulse — demo triage", owner, code, db.now_iso()),
        )

        slides = [
            (
                "mc",
                "Quale criterio dovrebbe guidare il triage?",
                {
                    "options": [
                        {"id": "a", "label": "Massimizzare gli anni di vita salvati"},
                        {"id": "b", "label": "Primo arrivato, primo servito"},
                        {"id": "c", "label": "Priorità ai più vulnerabili"},
                        {"id": "d", "label": "Sorteggio (lotteria equa)"},
                    ]
                },
            ),
            (
                "scale",
                "Quanto ti senti a tuo agio nel lasciare questa decisione a un algoritmo?",
                {"min": 1, "max": 5, "min_label": "Per niente", "max_label": "Del tutto"},
            ),
        ]
        slide_ids = []
        for i, (stype, q, cfg) in enumerate(slides, start=1):
            sid = db.new_id()
            conn.execute(
                "INSERT INTO slide (id, presentation_id, ord, type, question, config) VALUES (?,?,?,?,?,?)",
                (sid, pid, i, stype, q, json.dumps(cfg)),
            )
            slide_ids.append(sid)

        rid = db.new_id()
        conn.execute(
            "INSERT INTO run (id, presentation_id, label, active_slide_id, started_at) VALUES (?,?,?,?,?)",
            (rid, pid, "demo", slide_ids[0], db.now_iso()),
        )
        conn.execute("UPDATE presentation SET active_run_id=? WHERE id=?", (rid, pid))
        conn.execute(
            "INSERT INTO run_slide (run_id, slide_id, state) VALUES (?,?, 'open')",
            (rid, slide_ids[0]),
        )

    print("\n  RoomPulse demo seminata.")
    print(f"  Login     : http://localhost:8080/login   (spit@local / roompulse)")
    print(f"  Presenter : http://localhost:8080/present?p={pid}")
    print(f"  Audience  : http://localhost:8080/   codice {code}")
    print(f"  (oppure   : http://localhost:8080/?c={code} )\n")


if __name__ == "__main__":
    main()
