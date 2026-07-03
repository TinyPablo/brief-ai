# Brief AI — Project Notes

The single source of truth for what this project is and how it fits together.
Read this first before making changes.

## Concept

Brief AI is a tiny, self-hosted web app that gives quick, clear answers from
fast LLMs. One prompt in, one answer out — no chat threads, no history within a
conversation. Every prompt/answer pair (with timing, token usage, and cost) is
stored so the full history can be browsed later.

It is a personal tool: a single 6-digit PIN protects it, and a session cookie
keeps you logged in across tabs so you rarely need to re-enter the PIN.

## Design goals

- Feels lightweight and fast.
- Good-looking dark theme.
- Impossible to brute-force the PIN: server-side rate limiting of 3 seconds per
  attempt, per client IP.
- Deploys with a single command on a Linux server behind Cloudflare Tunnel +
  global Caddy: `git pull && docker compose up -d --build`.

## Architecture

```
Browser
  │
  ▼
Cloudflare Tunnel ──► global Caddy (on the host) ──► frontend (nginx :8080)
                                                       │  static SPA (React + Tailwind, CDN)
                                                       │  proxies /api/* ──► backend (Flask :5000)
                                                       │                        │
                                                       │                        ▼
                                                       │                     Anthropic API
                                                       │                        │
                                                       └────────────────────► PostgreSQL (:5432)
```

- **frontend** — nginx serving a single-file React SPA (no build step; React,
  Tailwind, marked, DOMPurify, highlight.js all loaded from CDN). nginx also
  reverse-proxies `/api/*` to the backend, so the browser only ever talks to one
  origin (cookies + CORS stay trivial). Published on `127.0.0.1:8080`.
- **backend** — Flask + gunicorn. Auth, rate limiting, Anthropic calls, history.
  Not published to the host; only reachable through the frontend proxy and the
  Docker network.
- **db** — PostgreSQL, data persisted in a named Docker volume (`db_data`).

The repo's `Caddyfile` is a snippet to paste into the host's **global** Caddy
config; it just reverse-proxies the site to `127.0.0.1:8080`. The real domain is
intentionally not committed.

## Auth & security

- Single shared PIN, `APP_PIN` (6 digits) from `.env`.
- Login compares with `hmac.compare_digest` (constant time).
- Rate limit: 3 s cooldown per client IP between login attempts (in-memory map,
  `429` with `retry_after` when exceeded). Client IP is taken from the leftmost
  `X-Forwarded-For` entry.
- Session is a signed Flask cookie (`SECRET_KEY`), `HttpOnly`, `SameSite=Lax`,
  `Secure` (configurable via `COOKIE_SECURE` for local http testing), 30-day
  lifetime.
- All `/api` routes except `login`/`session`/`logout`/`health` require auth.

## Data model

`prompts` table:

| column         | type          | notes                                  |
|----------------|---------------|----------------------------------------|
| id             | serial PK     |                                        |
| created_at     | timestamptz   | default now()                          |
| model          | text          | model that actually served the answer  |
| prompt         | text          |                                        |
| answer         | text          |                                        |
| input_tokens   | integer       |                                        |
| output_tokens  | integer       |                                        |
| cost_usd       | double        | computed from per-model pricing        |
| duration_ms    | integer       | round-trip time of the API call        |
| stop_reason    | text          | e.g. end_turn, max_tokens, refusal     |

## HTTP API

- `POST /api/login` `{pin}` → `{ok}` / `401` / `429 {retry_after}`
- `GET  /api/session` → `{authenticated}`
- `POST /api/logout` → `{ok}`
- `GET  /api/config` → `{models:[{id,label}], default}`  *(auth)*
- `POST /api/generate` `{model, prompt}` → answer + tokens + cost + duration *(auth)*
- `GET  /api/history` → last 100 rows (preview) *(auth)*
- `GET  /api/history/:id` → full row *(auth)*
- `GET  /api/health` → `{ok}`

## Models & pricing

Pricing is USD per 1M tokens (input / output). `effort` marks models that accept
`output_config.effort` (used at `low` to keep answers snappy — Haiku does not
support it).

| id                 | label      | input | output | effort |
|--------------------|------------|-------|--------|--------|
| claude-haiku-4-5   | Haiku 4.5  | 1     | 5      | no     |
| claude-sonnet-4-6  | Sonnet 4.6 | 3     | 15     | yes    |
| claude-opus-4-8    | Opus 4.8   | 5     | 25     | yes    |
| claude-fable-5     | Fable 5    | 10    | 50     | yes    |

Notes:
- Default model is **Sonnet 4.6**.
- Fable 5 always thinks and can be slow; it is called through the beta endpoint
  with a server-side fallback to Opus 4.8 on a policy refusal. The served model
  (which may be the fallback) is what gets priced and stored.
- Refusals (`stop_reason == "refusal"`) are surfaced as a short note.

## Adding more providers (future)

The plan is to add OpenAI (GPT) and Gemini models. The clean seam:

1. Add a provider abstraction in the backend — a function per provider that takes
   `(model, prompt)` and returns a normalized result
   `{answer, input_tokens, output_tokens, stop_reason}`.
2. Extend `MODELS` with a `provider` field and per-provider pricing; route
   `/api/generate` on that field.
3. Add the new API keys to `.env.example` (the user fills them in).
4. The frontend already renders whatever `/api/config` returns — no change needed
   beyond labels.

Keep the DB schema as-is; `model` already stores the served model id.

## Environment variables

See `.env.example`. Copy it to `.env` and fill it in. Never commit `.env`.
