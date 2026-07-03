# Brief AI

Brief AI delivers quick, clear answers with fast models and a simple interface.

One prompt in, one answer out — no chat threads. Every prompt and answer is
stored with its model, timing, token usage, and price, so you can browse the full
history later. The whole thing sits behind a 6-digit PIN with a persistent
session, so you log in once and stay in across tabs.

> Bootstrapped with Claude Opus 4.8.

## Features

- Dark, lightweight single-page interface.
- 6-digit PIN login with a persistent session cookie.
- Brute-force protection: 3-second server-side cooldown per login attempt.
- Model picker: **Haiku 4.5**, **Sonnet 4.6**, **Opus 4.8**, **Fable 5**.
- Rendered Markdown answers with syntax-highlighted code.
- Shows generation time, input/output tokens, and price per prompt.
- Prompt history stored in PostgreSQL and browsable in-app.

## Stack

- **Frontend** — React + Tailwind (loaded from CDN, no build step), served by nginx.
- **Backend** — Python / Flask + gunicorn, talking to the Anthropic API.
- **Database** — PostgreSQL.
- **Orchestration** — Docker Compose.

## Configuration

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable            | What it is                                                       |
|---------------------|-----------------------------------------------------------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key.                                         |
| `APP_PIN`           | The 6-digit login PIN.                                          |
| `SECRET_KEY`        | Long random string for signing session cookies.                |
| `COOKIE_SECURE`     | `true` in production (https); `false` only for local http tests.|
| `POSTGRES_*`        | Database name, user, and password.                             |

Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Run

```bash
docker compose up -d --build
```

The app is published on `127.0.0.1:8790`. The backend and database are not exposed
to the host — only the frontend, which proxies API calls internally.

## Deploy

On the server:

```bash
git clone <this-repo>
cd brief-ai
cp .env.example .env   # then fill it in
docker compose up -d --build
```

Updates:

```bash
git pull && docker compose up -d --build
```

### Reverse proxy

`Caddyfile` contains a snippet to paste into your host's global Caddy config. It
reverse-proxies your site to `127.0.0.1:8790`. Set your own domain in place of the
placeholder, reload Caddy, and point your Cloudflare Tunnel at Caddy as usual.

## History & data

Prompt history lives in the `db_data` Docker volume, so it survives rebuilds. To
wipe it, remove the volume:

```bash
docker compose down
docker volume rm brief-ai_db_data
```

## Notes

- Fable 5 always reasons and can take longer than the other models. It runs with a
  server-side fallback to Opus 4.8 if a request is refused; the served model is the
  one shown and priced.
- Prices are computed from published per-model rates and are estimates.
