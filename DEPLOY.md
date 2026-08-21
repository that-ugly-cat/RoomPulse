# Deploying RoomPulse

RoomPulse is a single FastAPI app backed by one SQLite file. It has no build step and no
external services — the only optional dependency is the **Anthropic API** (per-user key) for
argument clustering.

## 1. Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JWT_SECRET` | **yes, in production** | `dev-insecure-change-me` | signs the session cookie — set a long random value |
| `RP_DB` | no | `./roompulse.db` | path to the SQLite file (set this to a mounted volume in Docker) |
| `ANTHROPIC_API_KEY` | no | unset | **central** clustering key, spent by `full` and `admin` presenters. Leave unset and everyone pays with their own |
| `RP_ADMIN_EMAILS` | no | unset | addresses that get the `admin` role on registration |
| `RP_SIGNUP_CODE` | no | unset | if set, self-registration demands this code |
| `AUTH_MODE` | no | `local` | `local` = own login. `gateway` = trust an SSO gate in front (see §6) |
| `BORANT_TRUSTED_PROXY` | in `gateway` | `127.0.0.1` | the address the proxy connects from; headers from elsewhere are ignored |
| `BORANT_LOGOUT_URL` | no | `https://id.borant.eu/logout` | where "sign out" goes in `gateway` mode |

Generate a secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Who pays for clustering, and why it decides your role policy.** Presenters on
the `free` tier spend their own Claude key, set in the editor (⚙). Presenters on
`full` or `admin` spend `ANTHROPIC_API_KEY`, the server's own — usage is written
to `usage_log` and totalled in the admin panel, but **there is no ceiling in the
spending path**: the only thing standing between an account and your bill is the
role it was given. `free` is the registration default for exactly that reason,
and it is also what a profile created through the SSO gate gets, whatever the
gate suggests. Promote deliberately, never by default.

Nothing AI-related is needed to run the rest of the tool.

## 2. Local / bare-metal

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen                 # install pinned deps
export JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
uv run python seed.py            # first time only: creates DB + demo deck + demo user
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> On Windows, run **without** `--reload`: the reloader can leave orphan workers holding the port.

Users can also be created from the admin panel (`/admin`) once you have one admin: set
`RP_ADMIN_EMAILS` to the address you will register with, and that account is promoted on
startup. `RP_SIGNUP_CODE`, if set, gates self-registration behind a shared code — **without
it, anyone who reaches `/login` can create an account.**

Create real users and **change/remove the demo user** before going public:

```bash
uv run python create_user.py you@example.com 'a-strong-password' 'Your Name'
```

## 3. Docker

A `Dockerfile` is included. The DB lives at `RP_DB`; mount a volume so it survives restarts.

```bash
docker build -t roompulse .

docker run -d --name roompulse \
  -p 8080:8080 \
  -e JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e RP_DB=/data/roompulse.db \
  -v roompulse_data:/data \
  roompulse

# first run only: seed (or create a user) inside the container
docker exec roompulse uv run python seed.py
```

`docker-compose.yml`:

```yaml
services:
  roompulse:
    build: .
    restart: unless-stopped
    environment:
      JWT_SECRET: "change-me-to-a-long-random-string"
      RP_DB: /data/roompulse.db
    volumes:
      - roompulse_data:/data
    ports:
      - "8080:8080"
volumes:
  roompulse_data:
```

## 4. Reverse proxy (HTTPS)

Put it behind a proxy that terminates TLS. The audience joins over the public URL, so HTTPS
matters (QR codes point at it). Example **Caddy**:

```
yourdomain.example {
    reverse_proxy localhost:8080
}
```

The QR code is generated server-side from the request's host, so it will use whatever public
URL the proxy forwards.

## 5. Backups

The entire state (decks, runs, responses, users, API keys, clusters) is the one SQLite file
at `RP_DB`. Back up by copying it:

```bash
cp /var/lib/docker/volumes/roompulse_data/_data/roompulse.db backup-$(date +%F).db
```


## 6. Behind an SSO gate (`AUTH_MODE=gateway`)

Optional, and off unless you switch it on. It changes **only the presenter
side**. The audience is untouched and stays untouched: joining with a code or a
QR needs no account in either mode, which is the whole point of the tool.

In `gateway` RoomPulse stops checking presenter passwords and reads the identity
headers set by a `forward_auth` gate in front of it. `/login` redirects to
`/edit`, `/api/login` and `/api/register` refuse, and "sign out" sends the
browser to `BORANT_LOGOUT_URL` so the central session dies too.

**`local` stays the default.** An app that believes `X-Borant-Sub` with nothing
in front of it lets in anyone who sends that header.

Caddy. The cut is the sharpest in the estate and it is worth stating plainly:
everything the **audience** touches is public, everything the **presenter**
touches is gated, and the two do not interleave — `/api/live/*` is entirely
audience, `/api/runs/*` entirely presenter.

```
roompulse.borant.eu {
    @pubbliche path / /login /guide /static/* /qr/* /api/live/* /api/auth-config /api/i18n /api/login /api/register /api/logout
    handle @pubbliche {
        import noforge
        import nocookie
        reverse_proxy localhost:8011
    }
    handle {
        import borantid
        reverse_proxy localhost:8011
    }
}
```

`/` is the audience page and it loads its assets from `/static`, so keeping
`/static/*` out is correctness and not a speed tweak. `/login`, `/api/login` and
`/api/register` stay out because the app already refuses them in this mode:
gating them instead would answer a login attempt with a redirect to a different
login, which reads as a loop to whoever is looking at it.

**Before switching on, link the existing presenters.** Someone who is not linked
arrives as a *new* profile — without their presentations and on the starting
role:

```bash
docker exec roompulse-roompulse-1 python map_borant.py --map you@example.org=01ABC…
docker exec roompulse-roompulse-1 python map_borant.py --report
```

Read the report. The address someone uses here is not necessarily the address
the gate knows them by, and an email-based guess would quietly miss exactly
those people.

`BORANT_TRUSTED_PROXY` is the second lock and the setting people get wrong.
Under Docker the proxy runs on the host, so the container sees a bridge gateway
and not `127.0.0.1`. Read it off reality:

```bash
curl -s -o /dev/null http://127.0.0.1:8011/ && docker logs roompulse-roompulse-1 2>&1 | tail -1
```

Rollback, two lines and no data migration:

```bash
sed -i 's/^AUTH_MODE=gateway/AUTH_MODE=local/' .env
docker compose up -d
```
