# Brief AI

Brief AI delivers quick, clear answers with fast models and a simple interface.

One prompt in, one answer out - no chat threads. Every prompt and answer is
stored with its model, timing, token usage, and price, so you can browse the full
history later. The whole thing sits behind Google Authenticator (TOTP) login with a
persistent session, so you log in once and stay in across tabs.

> Bootstrapped with Claude Opus 4.8.

## Features

- Dark, lightweight single-page interface.
- Google Authenticator (TOTP) login with a persistent session cookie.
- Brute-force protection: 3-second server-side cooldown per attempt, plus a 5-minute
  lockout after 5 consecutive failures.
- Model picker grouped by provider - **Anthropic** (Haiku 4.5, Fable 5),
  **Google Gemini** (3.1 Flash Lite, 3.8 Flash), and **OpenAI** (GPT-5.4 Nano, GPT-6 Astra),
  each showing a per-prompt price estimate.
- Unified reasoning selector (Low-Max) mapped to each provider's own effort/thinking controls.
- Attach multiple images to a prompt (drag & drop, paste, or the file picker) on any model -
  up to 20 images, 10MB per image, 24MB total per prompt. Limits are enforced both in the
  browser and on the server.
- Optional prompt context (date, location, personal data) toggled in Settings and stored in the
  browser. It's stored in its own database column, never glued into the prompt, so History shows
  the question you actually asked and the personal bits stay separable.
- Live cost estimate (input only - text + attached images) from the prompt before sending.
- Rendered Markdown answers with LaTeX math, syntax-highlighted code, and per-block copy buttons.
- Shows generation time, input/output tokens, and price (in PLN) per prompt.
- Prompt history stored in PostgreSQL - grouped by week/day (collapsible), full-text
  search, infinite scroll, and per-conversation delete.

## Stack

- **Frontend** - React + Tailwind (loaded from CDN, no build step), served by nginx.
- **Backend** - Python / Flask + gunicorn, talking to the Anthropic API.
- **Database** - PostgreSQL.
- **Orchestration** - Docker Compose.

## Configuration

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable            | What it is                                                       |
|---------------------|-----------------------------------------------------------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key.                                         |
| `GEMINI_API_KEY`    | Your Google AI Studio API key.                                 |
| `TOTP_SECRET`       | Secret for Google Authenticator login (see below).              |
| `SECRET_KEY`        | Long random string for signing session cookies.                |
| `COOKIE_SECURE`     | `true` in production (https); `false` only for local http tests.|
| `POSTGRES_*`        | Database name, user, and password.                             |

Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Enrolling Google Authenticator

Run this **locally**, not on the deploy server, so the secret and QR code
never travel over the network:

```bash
pip install -r backend/requirements-totp-setup.txt
python backend/generate_totp_secret.py
```

Scan the printed QR code with Google Authenticator (or any TOTP app), then
paste the printed `TOTP_SECRET=...` line into your `.env`.

If you lose your phone, SSH into the server and rerun the steps above to
generate a new secret, then update `.env` and restart the backend.

## Run

```bash
docker compose up -d --build
```

The app is published on `127.0.0.1:8790`. The backend and database are not exposed
to the host - only the frontend, which proxies API calls internally.

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

- Fable 5 always reasons and can take longer than the other models. It runs with
  Anthropic's default server-side fallback routing if a request is refused; the served
  model is the one shown and priced.
- The model picker shows six models, but `MODELS` in `backend/app.py` is a larger catalog
  that also keeps retired ones. History stores whichever model served each answer, so a
  retired id still needs its label and its rate to render and price correctly - retiring a
  model means removing it from `MODEL_ORDER`, never from `MODELS`.
- Prices are computed from published per-model rates and shown in PLN at a fixed USD→PLN
  rate. The per-model estimate in the dropdown assumes a typical prompt of ~1000 input +
  ~1500 output tokens. Both the rate and the assumption are constants in `backend/app.py`.
  Gemini 3.8 Flash is listed at its standard rate, not the introductory one running until
  2026-12-31, so estimates never understate what it will cost from January.
- Image attachments aren't blocked on any model - attaching to a pricier model just costs
  more, which is a deliberate tradeoff, not a bug. Attached images are sent with the
  request but never stored: they're not persisted to the database, so they won't appear
  when you revisit a prompt in History.
