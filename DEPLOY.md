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
| `PUBLIC_URL` | for MCP | unset | the public origin, e.g. `https://roompulse.example`. The MCP transport checks the `Host` header against DNS rebinding, so without this every proxied MCP request is refused |
| `BORANT_TRUSTED_PROXY` | in `gateway` | `127.0.0.1` | the address the proxy connects from; headers from elsewhere are ignored |
| `BORANT_LOGOUT_URL` | no | `https://id.borant.eu/logout` | where "sign out" goes in `gateway` mode |
| `PROVISION_SECRET` | no | unset | shared secret for `/internal/provision` (see §7). Unset = the route does not exist |
| `PROVISION_TRUSTED` | no | unset | CIDR the gate calls from, e.g. `172.28.0.0/16`. Unset = the route does not exist |

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

## The landing, the home, and the role hint

**RoomPulse is the exception to the perimeter's shape.** Everywhere else `/` is
a public showcase and the app lives at `/app`. Here `/` is already public and
already the product: it is the audience page, where a room joins with a code
and no account at all. The host's side lives at `/edit` and `/present`, behind
the gate.

**The role hint is honoured**, with a vocabulary of `free, full, admin`. Until
24/8/2026 the gate declared three and the code accepted exactly one, which is a
menu offering roles the code ignores — worse than no menu. `full` and `admin`
cluster with the server's central Anthropic key, so they spend: creating a
profile in either role from a hint is logged loudly, naming the address and the
subject. An unrecognised hint is a typo, not a role, and falls back to `free`.

**A page that needs an identity fails closed.** In `gateway` an unauthenticated
request to `/edit`, `/present` or `/admin` does *not* redirect to `/login` — the
app switches that route off in this mode and sends it back to `/edit`, so the
two would bounce forever. Production never shows it because the gate intercepts
first, but a wrong proxy matcher would produce a spin instead of an error. The
answer is a 503 naming what the operator should check.

## 6-bis. Provisioning in advance (`/internal/provision`)

Optional, and off unless you turn it on. Without it a presenter's profile is
born the first time they open RoomPulse; with it, Borant ID says who is coming
as soon as it grants access, so the profile is already here and can be prepared
against — and a hundred people arriving in the same minute stop racing each
other to create their own rows.

**The route is not reachable from the internet, by construction.** It is not a
public path in the Caddy config and it never goes through Caddy at all: the gate
calls the container directly on a docker network the two share. Two locks, and
if either is missing the route answers 404 as though it did not exist:

- `PROVISION_SECRET` — the credential, compared in constant time.
- `PROVISION_TRUSTED` — the CIDR the gate's container sits on.

### Wiring it up

1. **One network for both containers.** The published ports stay on
   `127.0.0.1` (that invariant does not move); this is a second, internal
   network where the two can address each other by name.

   ```bash
   docker network create borant_provision
   docker network inspect borant_provision -f '{{(index .IPAM.Config 0).Subnet}}'
   ```

   The subnet that prints is what goes in `PROVISION_TRUSTED`. Read it, do not
   assume it: docker picks the range, and `172.17.0.0/16` is the *default
   bridge*, which is not this network.

   **Adding this network changes the address the proxy appears to come from,
   and that is how you lock everyone out.** Docker picks among the gateways of
   a container's networks in alphabetical order of network name, so
   `borant_provision` sorts ahead of `roompulse_default` and Caddy's requests
   started arriving from `192.168.240.1` instead of `192.168.0.1`. With
   `BORANT_TRUSTED_PROXY` still naming the old one, the app threw the gate's
   headers away and nobody could sign in — measured in production on 8 Sep
   2026, three `X-Borant-Sub from 192.168.240.1, outside BORANT_TRUSTED_PROXY`
   lines in the log. So, in the same breath as the network:

   ```
   BORANT_TRUSTED_PROXY=192.168.0.1,192.168.240.1
   ```

   The field has always taken a comma-separated list. Keep both: the ordering
   can flip on the next `up -d` with nobody having touched anything. And do not
   check this by loading a page — a gated path answers 302 both when you are
   not signed in and when the app has discarded who you are. Check it with a
   real session, and read the app's log.

2. **Join both compose files to it**, RoomPulse's and Borant ID's:

   ```yaml
   services:
     roompulse:
       networks: [default, borant_provision]
   networks:
     borant_provision:
       external: true
   ```

3. **Set the two variables** in RoomPulse's `.env` and restart. The secret is
   any long random string; generate it the same way as `JWT_SECRET`.

4. **On the gate**, in `/admin/apps` → RoomPulse, fill in the provisioning
   address and the same secret. The address is the container on the shared
   network and the port it listens on *inside* the container, which is 8080 and
   not the 8011 published on the host:

   ```
   http://roompulse:8080/internal/provision
   ```

   Then press «Resync»: it pushes everyone who already has a grant, and the
   numbers it reports are the proof the wiring works.

### What it may and may not do

**It creates profiles that are not there, and nothing else.** It never updates a
profile, never changes a role, never deactivates. A stolen secret buys empty
accounts, not somebody's presentations — which is the difference between a
convenience and a remote control on this database.

**It does not link by address.** If a local profile already holds an incoming
address without a `borant_sub`, the entry is reported back as a conflict and
nothing is touched. Linking those is `map_borant.py`, by hand, as it always was:
one typo in the gate's panel must not merge two accounts, and that does not
become safer for happening at office hours.

## 7. The MCP surface

`/mcp` lets a model read decks, runs and the live room, and compose a deck. It is mounted on
the same app and needs nothing extra installed — but two things have to be right, and both
are easy to get wrong in a way that only shows up from outside.

**`PUBLIC_URL` must be set.** The MCP transport validates the `Host` header against DNS
rebinding. Localhost is allowed with any port for development; the public domain is not
guessed, and if it is missing every request through the proxy comes back `Invalid Host
header` while the same call works on the box.

**`/mcp` must skip the SSO gate.** Its own credential *is* the per-user API key, and a model
has no browser with which to satisfy a `forward_auth` challenge. In Caddy that means
matching the path before the gated block:

```
roompulse.example {
    @mcp path /mcp /mcp/*
    handle @mcp {
        reverse_proxy localhost:8080
    }
    # ...the gated handlers for everything else
}
```

Skipping the gate is not skipping authentication: without a valid key `/mcp` answers 401
before the request reaches the MCP layer at all, and a key resolves to exactly one user, so
a call reaches exactly the decks that user owns.

**Keys** are issued per user from the editor's ⚙ panel, shown once, and revocable. They live
in `mcp_key` and have nothing to do with `user.api_key`, which is the Anthropic key for
clustering — different purpose, different table, and confusing the two would hand a model
the ability to spend money.
