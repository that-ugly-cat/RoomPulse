# Deploying RoomPulse

RoomPulse is a single FastAPI app backed by one SQLite file. It has no build step and no
external services — the only optional dependency is the **Anthropic API** (per-user key) for
argument clustering.

## 1. Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JWT_SECRET` | **yes, in production** | `dev-insecure-change-me` | signs the session cookie — set a long random value |
| `RP_DB` | no | `./roompulse.db` | path to the SQLite file (set this to a mounted volume in Docker) |

Generate a secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The Claude API key for clustering is **not** an env var — each presenter sets their own in
the editor (⚙). Nothing AI-related is needed to run the rest of the tool.

## 2. Local / bare-metal

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen                 # install pinned deps
export JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
uv run python seed.py            # first time only: creates DB + demo deck + demo user
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> On Windows, run **without** `--reload`: the reloader can leave orphan workers holding the port.

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
roompulse.example.org {
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

## 6. Pre-flight checklist

- [ ] `JWT_SECRET` set to a long random value
- [ ] demo user (`spit@local`) removed or its password changed
- [ ] real presenter account(s) created
- [ ] HTTPS in front, public hostname resolves
- [ ] DB on a persistent volume, backups scheduled
- [ ] (optional) each presenter has added their Anthropic API key in ⚙ for clustering
