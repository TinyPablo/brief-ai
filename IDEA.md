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
Cloudflare Tunnel ──► global Caddy (on the host) ──► frontend (nginx :8790)
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
  origin (cookies + CORS stay trivial). Published on `127.0.0.1:8790`.
- **backend** — Flask + gunicorn. Auth, rate limiting, Anthropic calls, history.
  Not published to the host; only reachable through the frontend proxy and the
  Docker network.
- **db** — PostgreSQL, data persisted in a named Docker volume (`db_data`).

The repo's `Caddyfile` is a snippet to paste into the host's **global** Caddy
config; it just reverse-proxies the site to `127.0.0.1:8790`. The real domain is
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
| reasoning      | text          | reasoning level used, or null if n/a   |

## HTTP API

- `POST /api/login` `{pin}` → `{ok}` / `401` / `429 {retry_after}`
- `GET  /api/session` → `{authenticated}`
- `POST /api/logout` → `{ok}`
- `GET  /api/config` → `{models:[{id,label,provider,provider_label,input,output,controls,fixed,est_pln}], default, usd_pln, max_tokens}` *(auth)*
- `POST /api/generate` `{model, prompt, effort, thinking}` → answer + tokens + cost + duration *(auth)*
- `GET  /api/history?q=&before=` → page of rows (preview, 30 per page, newest first); `q`
  filters prompt+answer (ILIKE), `before` is an id cursor for infinite scroll *(auth)*
- `GET  /api/history/:id` → full row *(auth)*
- `DELETE /api/history/:id` → delete a row *(auth)*
- `GET  /api/health` → `{ok}`

## Models & pricing

Two providers, selected per model via a `provider` field. Pricing is USD per 1M
tokens (input / output). `effort` (Anthropic only) marks models that accept
`output_config.effort`, used at `medium` (balanced speed vs. thoroughness).

| id                     | label               | provider | input | output | effort |
|------------------------|---------------------|----------|-------|--------|--------|
| gemini-2.5-flash-lite  | Gemini 2.5 Flash Lite | google   | 0.10  | 0.40   | —      |
| gemini-3.1-flash-lite  | Gemini 3.1 Flash Lite | google   | 0.25  | 1.50   | —      |
| gemini-3.5-flash       | Gemini 3.5 Flash    | google   | 1.50  | 9.00   | —      |
| claude-haiku-4-5       | Haiku 4.5           | anthropic| 1     | 5      | no     |
| claude-sonnet-4-6      | Sonnet 4.6          | anthropic| 3     | 15     | yes    |
| claude-opus-4-8        | Opus 4.8            | anthropic| 5     | 25     | yes    |
| claude-fable-5         | Fable 5             | anthropic| 10    | 50     | yes    |

Notes:
- Default model is **`gemini-3.1-flash-lite`**.
- The UI groups the model dropdown by provider (Anthropic / Google Gemini) and
  shows a per-prompt price estimate next to each model.
- **Price display is in PLN.** Cost is computed in USD from real token counts, then
  the frontend multiplies by `USD_TO_PLN` (constant in `backend/app.py`, currently
  `4.0`) for display. The DB stores `cost_usd` (USD stays the source of truth).
- The dropdown estimate assumes a typical prompt of `ESTIMATE_INPUT_TOKENS` (1000)
  + `ESTIMATE_OUTPUT_TOKENS` (1500) tokens; `/api/config` returns the pre-computed
  `est_pln` per model plus the `usd_pln` rate.
- Fable 5 always thinks and can be slow; it is called through the beta endpoint
  with a server-side fallback to Opus 4.8 on a policy refusal. The served model
  (which may be the fallback) is what gets priced and stored.
- Anthropic refusals (`stop_reason == "refusal"`) and empty/blocked Gemini
  responses are surfaced as a short note.

## Reasoning controls (per model)

`/api/config` returns per-model `controls` (interactive dropdowns) and `fixed`
(read-only labels). The frontend renders whatever the model exposes; defaults are
always the lowest. `/api/generate` takes `effort` and `thinking`.

- **Anthropic Sonnet 4.6 / Opus 4.8**: `Effort` (low/medium/high/max, default low →
  `output_config.effort`) + `Thinking` (off/on, default off → `thinking:
  {type:"adaptive"}` when on).
- **Anthropic Fable 5**: `Effort` selectable; `Thinking` fixed "On" (always thinks).
- **Anthropic Haiku 4.5**: no controls (shown as "Default").
- **Gemini 3.x**: single `Effort` (low/medium/high → `thinking_config.thinking_level`,
  built defensively; `max` → `high`).
- **Gemini 2.5**: fixed "Effort: Off" (native thinking off).

The applied combination is summarised into the `reasoning` column (e.g. `Low`,
`Medium · thinking`) and shown in history.

## Prompt context (client-side settings)

The Settings tab stores a small object in the browser (`localStorage`, key
`briefai_settings`) and the frontend prepends context lines above the prompt before
sending. Nothing about this is server-side; the composed text is what gets stored in
history.

- `now: <date>, <HH:MM>` — single "Date & time" toggle (default on), from the browser clock.
- `user is currently in <text>` — editable location (default "Bielsko-Biała").
- `user data: <text>` — free-form personal context (default off).

Composition: `<context>\n\n` + optional `very brief: ` (Brief button) + the prompt.
Each part is independently toggleable; all off → just the raw prompt.

## Live cost estimate

Shown next to the input as `est. ~X zł`. Input tokens ≈ composed-text length / 4;
output assumed 1.5× input, capped at `max_tokens`. Priced from the model's per-1M
rates × `USD_TO_PLN`. Thinking/effort overhead is not modelled — it's a rough guide
(the exact cost is shown after the answer).

## Adding more providers

The provider seam already exists (Anthropic + Google Gemini). `MODELS[id]` has a
`provider` field; `/api/generate` routes to `call_anthropic` or `call_gemini`, each
returning a normalized dict `{answer, input_tokens, output_tokens, stop_reason,
served}`. To add another provider (e.g. OpenAI):

1. Add a `call_<provider>` helper returning that same dict shape.
2. Add its models to `MODELS` (with `provider`, pricing) and `MODEL_ORDER`, plus a
   `PROVIDER_LABELS` entry.
3. Route to the new helper in `generate()`.
4. Add its API key to `.env.example`, `docker-compose.yml`, and requirements.
5. The frontend groups the dropdown from `provider_label` automatically — no change
   needed beyond what `/api/config` returns.

Keep the DB schema as-is; `model` already stores the served model id.

## Environment variables

See `.env.example`. Copy it to `.env` and fill it in. Never commit `.env`.
