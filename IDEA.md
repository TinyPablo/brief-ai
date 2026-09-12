# Brief AI - Project Notes

The single source of truth for what this project is and how it fits together.
Read this first before making changes.

## Concept

Brief AI is a tiny, self-hosted web app that gives quick, clear answers from
fast LLMs. One prompt in, one answer out - no chat threads, no history within a
conversation. Every prompt/answer pair (with timing, token usage, and cost) is
stored so the full history can be browsed later.

It is a personal tool: Google Authenticator (TOTP) protects it, and a session
cookie keeps you logged in across tabs so you rarely need to re-authenticate.

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

- **frontend** - nginx serving a single-file React SPA (no build step; React,
  Tailwind, marked, DOMPurify, highlight.js all loaded from CDN). nginx also
  reverse-proxies `/api/*` to the backend, so the browser only ever talks to one
  origin (cookies + CORS stay trivial). Published on `127.0.0.1:8790`.
- **backend** - Flask + gunicorn. Auth, rate limiting, Anthropic calls, history.
  Not published to the host; only reachable through the frontend proxy and the
  Docker network.
- **db** - PostgreSQL, data persisted in a named Docker volume (`db_data`).

The repo's `Caddyfile` is a snippet to paste into the host's **global** Caddy
config; it just reverse-proxies the site to `127.0.0.1:8790`. The real domain is
intentionally not committed.

## Auth & security

- Google Authenticator (TOTP), `TOTP_SECRET` (base32) from `.env` - generated locally
  with `backend/generate_totp_secret.py`, never on the deploy server.
- Rate limit: 3 s cooldown per client IP between login attempts, plus a 5-minute lockout
  after 5 consecutive failures (in-memory maps, `429` with `retry_after` when exceeded).
  Client IP is taken from the leftmost `X-Forwarded-For` entry.
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
- `GET  /api/config` → `{models:[{id,label,provider,provider_label,input,output,context_window,depth,est_pln}], default, usd_pln, max_tokens, image_limits:{max_images,max_image_mb,max_total_mb,allowed_types}}` *(auth)*
- `POST /api/generate` `{model, prompt, depth, images?}` → answer + tokens + cost + duration *(auth)*.
  `images` is an optional array of `{data (base64, no data: prefix), media_type}`, validated
  server-side against `image_limits` before any provider is called.
- `GET  /api/history?q=&before=` → page of rows (preview, 30 per page, newest first); `q`
  filters prompt+answer (ILIKE), `before` is an id cursor for infinite scroll *(auth)*
- `GET  /api/history/:id` → full row *(auth)*
- `DELETE /api/history/:id` → delete a row *(auth)*
- `GET  /api/health` → `{ok}`

## Models & pricing

Three providers, selected per model via a `provider` field. Pricing is USD per 1M
tokens (input / output). `context_window` (tokens) is shown in the Ask view's
session sidebar.

| id                     | label               | provider | input | output | context_window |
|------------------------|---------------------|----------|-------|--------|-----------------|
| gemini-2.5-flash-lite  | Gemini 2.5 Flash Lite | google   | 0.10  | 0.40   | 1M              |
| gemini-3.1-flash-lite  | Gemini 3.1 Flash Lite | google   | 0.25  | 1.50   | 1M              |
| gemini-3.5-flash       | Gemini 3.5 Flash    | google   | 1.50  | 9.00   | 1M              |
| claude-haiku-4-5       | Haiku 4.5           | anthropic| 1     | 5      | 200k            |
| claude-sonnet-4-6      | Sonnet 4.6          | anthropic| 3     | 15     | 200k            |
| claude-opus-4-8        | Opus 4.8            | anthropic| 5     | 25     | 200k            |
| claude-fable-5         | Fable 5             | anthropic| 10    | 50     | 200k            |
| gpt-6-astra            | GPT-6 Astra         | openai   | 10    | 50     | 400k            |

Notes:
- Default model is **`gemini-3.1-flash-lite`**.
- The UI groups the model dropdown by provider (Anthropic / Google Gemini / OpenAI)
  and shows a per-prompt price estimate next to each model.
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

`/api/config` returns per-model `effort` and `thinking` descriptors, each with an
`available` flag. The UI always shows an Effort control (dropdown when available,
read-only value otherwise) and a Thinking switch (interactive when available, locked
to its fixed value otherwise). Defaults are always the lowest. `/api/generate` takes
`effort` and `thinking`.

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

- `now: <date>, <HH:MM>` - single "Date & time" toggle (default on), from the browser clock.
- `user is currently in <text>` - editable location (default "Bielsko-Biała").
- `user data: <text>` - free-form personal context (default off).

Composition: `<context>\n\n` + optional `very brief: ` (Brief button) + the prompt.
Each part is independently toggleable; all off → just the raw prompt.

## Live cost estimate

Shown next to the input as `est_in: X zł`, priced from the model's per-1M input
rate × `USD_TO_PLN`. **Input only** - the output side isn't estimated, since it
can't be predicted before the model answers (the exact total cost, input + output,
is shown after the answer). Input tokens ≈ composed-text length / 4, plus, for any
attached images, `ceil(width/28) * ceil(height/28)` per image (Anthropic's public
patch-token formula, used as a rough cross-provider guide - exact tokenization
differs per provider). Thinking/effort overhead is not modelled either.

## Image attachments

The Ask view lets you attach multiple images to a prompt (drag & drop onto the
composer, paste from the clipboard, or the attach button's file picker). Works on
every model, on any provider - there's no cost gate, attaching to a pricier model
is just a more expensive prompt.

- Limits: **20 images max**, **10MB per image**, **24MB total** per prompt. Enforced
  in the browser (reading the numbers from `/api/config`'s `image_limits`, so
  there's one source of truth) **and** on the server (`validate_images()` in
  `backend/app.py`) - the server never trusts the client's count.
- Each image is sent as `{data: base64, media_type}` in `/api/generate`'s `images`
  array, decoded server-side, and passed to whichever provider's SDK as its own
  multi-part content shape (image blocks for Anthropic, `Part.from_bytes` for
  Gemini, `image_url` data URIs for OpenAI's Chat Completions API).
- Not persisted: images live only for the duration of the request. The `prompts`
  table has no image column, so History has no way to show them - this was a
  deliberate scope cut, not an oversight.

## Adding more providers

The provider seam already exists (Anthropic + Google Gemini + OpenAI). `MODELS[id]`
has a `provider` field; `/api/generate` routes to `call_anthropic`, `call_gemini`, or
`call_openai`, each taking `(model, prompt, depth, images=None)` and returning a
normalized dict `{answer, input_tokens, output_tokens, stop_reason, served}`. To add
another provider:

1. Add a `call_<provider>` helper returning that same dict shape.
2. Add its models to `MODELS` (with `provider`, pricing) and `MODEL_ORDER`, plus a
   `PROVIDER_LABELS` entry.
3. Route to the new helper in `generate()`.
4. Add its API key to `.env.example`, `docker-compose.yml`, and requirements.
5. The frontend groups the dropdown from `provider_label` automatically - no change
   needed beyond what `/api/config` returns.

Keep the DB schema as-is; `model` already stores the served model id.

## Environment variables

See `.env.example`. Copy it to `.env` and fill it in. Never commit `.env`.
