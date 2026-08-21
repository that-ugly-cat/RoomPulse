"""
Lega i presenter esistenti al subject con cui un gate SSO li conosce.

Si esegue una volta sola, a mano, PRIMA di mettere AUTH_MODE=gateway, e il
report si legge prima di crederci:

    docker exec roompulse-roompulse-1 python map_borant.py \
        --map tu@example.org=01ABC... --map altra@example.org=01DEF...
    docker exec roompulse-roompulse-1 python map_borant.py --report

Perche' uno script e non un aggancio automatico per email: legare per indirizzo
a runtime e' difendibile — l'email arriva dal gate, non dal client — ma
significa che un errore di battitura nel pannello del gate fonde due account
senza che nessuno se ne accorga. Uno script si legge prima di lanciarlo e
stampa cosa ha fatto.

Perche' le coppie sono argomenti invece di essere cercate: quest'app non parla
mai col gate. Non sa da dove vengano i subject, e aggiungere una lookup la
renderebbe dipendente da un servizio senza il quale deve continuare a girare.
Le coppie si producono sull'host e si incollano qui.

**Il caso che rende questo script necessario e non decorativo**: l'indirizzo con
cui una persona sta qui puo' NON essere quello con cui sta nel gate. Chi ha
aperto l'account con una casella personale e nel gate compare con quella
istituzionale non verrebbe mai ritrovato da un aggancio per email — entrerebbe
e si troverebbe un profilo nuovo, senza le sue presentazioni e senza il suo
ruolo. Con le coppie esplicite quel caso si scrive e si vede.

Niente qui e' distruttivo: un legame gia' presente si segnala e non si
sovrascrive, e per staccarlo c'e' --unlink.
"""
import argparse
import sys

from app import db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="append", default=[], metavar="EMAIL=SUBJECT",
                    help="lega un presenter locale a un subject del gate; ripetibile")
    ap.add_argument("--unlink", action="append", default=[], metavar="EMAIL",
                    help="stacca il legame di un presenter; ripetibile")
    ap.add_argument("--report", action="store_true",
                    help="stampa chi e' legato e chi no, senza cambiare niente")
    args = ap.parse_args()

    conn = db.get_conn()
    changed = 0

    for pair in args.map:
        email, sep, subject = pair.partition("=")
        email, subject = email.strip().lower(), subject.strip()
        if not sep or not email or not subject:
            print(f"  SALTO     {pair!r}: serve la forma email=subject")
            continue
        row = conn.execute("SELECT id, email, role, borant_sub FROM user WHERE lower(email)=?",
                           (email,)).fetchone()
        if row is None:
            print(f"  ASSENTE   {email}: nessun presenter con questo indirizzo")
            continue
        if row["borant_sub"] == subject:
            print(f"  GIA-OK    {email} -> {subject}")
            continue
        if row["borant_sub"]:
            print(f"  CONFLITTO {email}: gia' legato a {row['borant_sub']}, non sovrascrivo. "
                  f"Usa --unlink prima, se e' voluto.")
            continue
        clash = conn.execute("SELECT email FROM user WHERE borant_sub=?", (subject,)).fetchone()
        if clash is not None:
            print(f"  CONFLITTO {email}: il subject {subject} e' gia' di {clash['email']}")
            continue
        conn.execute("UPDATE user SET borant_sub=? WHERE id=?", (subject, row["id"]))
        changed += 1
        print(f"  LEGATO    {email} ({row['role']}) -> {subject}")

    for email in args.unlink:
        email = email.strip().lower()
        row = conn.execute("SELECT id, borant_sub FROM user WHERE lower(email)=?",
                           (email,)).fetchone()
        if row is None or not row["borant_sub"]:
            print(f"  NIENTE    {email}: non era legato")
            continue
        print(f"  SLEGATO   {email} (era {row['borant_sub']})")
        conn.execute("UPDATE user SET borant_sub=NULL WHERE id=?", (row["id"],))
        changed += 1

    if changed:
        conn.commit()

    print("\n-- stato dei presenter --")
    rows = conn.execute(
        "SELECT email, role, is_active, borant_sub, "
        "  (SELECT COUNT(*) FROM presentation p WHERE p.owner=user.id) AS n "
        "FROM user ORDER BY role, email").fetchall()
    scoperti = []
    for r in rows:
        stato = r["borant_sub"] or "(nessun legame)"
        print(f"  {r['role']:<6} {r['email']:<34} deck={r['n']:<3} {stato}")
        if not r["borant_sub"] and r["is_active"]:
            scoperti.append(r)
    print(f"\n  {len(rows)} presenter, {len(scoperti)} attivi senza legame.")
    if scoperti:
        print("  In `gateway` chi non ha un legame arriva come profilo NUOVO — quindi")
        print("  senza le sue presentazioni e col ruolo di partenza. Se non e' quello")
        print("  che vuoi, legalo prima di accendere.")
        persi = sum(r["n"] for r in scoperti)
        if persi:
            print(f"  Attenzione: fra loro ci sono {persi} presentazioni che resterebbero")
            print("  attaccate a un profilo che nessuno raggiunge piu' dal gate.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
