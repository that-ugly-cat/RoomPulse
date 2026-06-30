"""Crea un utente presentatore.

    uv run python create_user.py <email> <password> [nome]

In produzione esporta JWT_SECRET prima di avviare il server.
"""

import sys

from app import db, auth


def main():
    if len(sys.argv) < 3:
        print("uso: uv run python create_user.py <email> <password> [nome]")
        sys.exit(1)
    email, password = sys.argv[1], sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else email.split("@")[0]
    db.init_db()
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM user WHERE email=?", (email,)).fetchone():
            print(f"utente {email} esiste gia'.")
            return
        conn.execute(
            "INSERT INTO user (id, email, password_hash, name, is_active, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (db.new_id(), email, auth.hash_password(password), name, db.now_iso()),
        )
    print(f"utente {email} creato.")


if __name__ == "__main__":
    main()
