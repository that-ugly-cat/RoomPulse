<p align="center">
  <img src="app/static/imgs/logo.png" alt="RoomPulse" width="360">
</p>

<p align="center">
  <b>Live polling for talks and lectures.</b><br>
  Your audience answers from their phones; results appear as you speak.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: AGPL v3" src="https://img.shields.io/badge/License-AGPLv3-blue.svg"></a>
</p>

---

RoomPulse is a self-hosted live polling tool, built for keynotes and
teaching — with a focus on **argumentative and ethical questions**, not just trivia (but trivia are fun and who am I to judge so there's also a quiz mode). The
audience joins from any browser with a code or QR (no app, no login); the presenter drives
the deck and sees results update live. Try it live [here](https://roompulse.borant.eu/login).

## Features

- **11 question types**: single/multiple choice (with quiz mode and participant-added
  options), Likert scale, word cloud, 2×2 quadrant, ranking (drag & drop), points
  allocation, open text, Q&A with upvotes, *claim + justification*, *argument chain*,
  and a random group splitter.
- **Donut energizer** — a one-button endless-runner (tap / spacebar) played on the audience's
  phones: dodge the ruins, grab donuts, and everyone's best scores build a live leaderboard.
  A break that *also* produces data — handy when the talk is about attention or human factors.
- **Argument chain** — build an argument one move at a time across linked slides: first a
  claim, then the justification for it, then an objection — each stage carrying the previous
  answers over. The objection stage can hand each participant *someone else's* argument
  (stable, balanced, never your own), which is what yields several objections on the same
  claim. The result is a table of claim → justification → objections; objections raised aloud
  in the room can be typed straight into it.
- **Quiz mode** — mark correct answers; participants see if they got it right (the answer is
  never leaked before they respond).
- **Pre/post** — ask the same question before and after your argument and show the shift
  (mean for scales, distribution for choice, centroid arrow for the quadrant).
- **AI argument clustering** — for *claim + justification*, *open text* and *argument chain*
  questions, an LLM groups the responses into descriptive themes (two axes for
  claim+justification: the criterion **and** the kind of appeal, with a criterion × appeal
  matrix; for the argument chain: criterion **and** kind of objection). Each presenter uses
  their own Anthropic API key.
- **User management** — admins create users, reset passwords, deactivate or delete accounts
  from the admin panel; everyone can change their own password. A password change or reset
  drops that account's other open sessions, and the last admin can't be locked out.
- **Live moderation** for free-text answers, **per-owner isolation** (each user only sees
  their own decks), **deck export/import** (JSON) and **data export** (Excel — one sheet per run).
- **Trilingual UI** — Italian, English, German.

## How it works

Three surfaces, one fixed join code per deck:

| Surface | Who | What |
|---|---|---|
| **Editor** | presenter | build decks, add/reorder/inspect slides, set the API key |
| **Presenter** | presenter | the projection: active question, live results, controls, headcount, QR + code |
| **Audience** | public | enter the code / scan the QR, answer, see results when revealed |

The audience follows the presenter via light polling (no websockets). A deck (template) can
be run many times; each *run* keeps its own responses. That same poll doubles as a presence
heartbeat, so the presenter sees how many people are **in the room** next to how many have
actually **voted** — distinct people, not rows, which is not the same number as soon as a
question takes multiple answers or repeated submissions.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it manages Python and dependencies).

```bash
git clone https://github.com/that-ugly-cat/RoomPulse.git
cd RoomPulse

# create the DB + a demo deck and a demo user (spit@local / roompulse)
uv run python seed.py

# run
uv run uvicorn app.main:app --port 8080
```

Then open:

- **Login / editor** — http://localhost:8080/login  (demo: `spit@local` / `roompulse`)
- **Audience** — http://localhost:8080/  (the seed prints the join code)

Create your own presenter account:

```bash
uv run python create_user.py you@example.com yourpassword "Your Name"
```

To enable AI clustering, open the editor, click **⚙**, and paste your Anthropic API key
(stored per user). Get one at <https://console.anthropic.com/>.

## Stack

FastAPI · SQLite · vanilla-JS frontend (static, polling) · `uv`. No build step.

## Deployment

See **[DEPLOY.md](DEPLOY.md)** for production setup (environment variables, Docker, reverse
proxy, backups).

## Tech notes

- Audience endpoints are public (by join code); everything else requires a presenter login.
- Set `JWT_SECRET` in production (there is an insecure default for local dev).
- The whole database is a single SQLite file — back up by copying it.

## License

Copyright (C) 2026 Giovanni Spitale. Licensed under AGPL-3.0 — fork it, host it, sell access
to it, but keep it closed-source and you're in violation. No SaaS forks that don't share
back. See [LICENSE](LICENSE).

## Optional: behind an SSO gate

`AUTH_MODE=gateway` hands presenter identity to an upstream `forward_auth` gate
instead of the local password: presenters arrive already signed in, `/login`
switches itself off, and accounts are matched by an immutable subject rather
than by email address.

**The audience never meets any of it.** The join page, the QR codes and the
whole `/api/live/*` surface stay open — a room full of phones is exactly the
population that must not be asked to sign in, which is why the public/private
split runs where it does.

One thing worth knowing if you enable it: a presenter arriving for the first
time is provisioned on the **free** tier, which uses their own LLM key. Roles
that spend the server's key are granted by hand, never by arriving.

`local` is the default and stays fully supported. Details in `DEPLOY.md`.
