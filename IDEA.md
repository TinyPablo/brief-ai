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
| prompt         | text          | **what the user typed, nothing else**  |
| context        | text          | Settings context block, or null        |
| brief          | boolean       | the Brief button's prefix was applied  |
| prompt_is_raw  | boolean       | false on rows written before the split |
| answer         | text          |                                        |
| input_tokens   | integer       |                                        |
| output_tokens  | integer       |                                        |
| cost_usd       | double        | computed from per-model pricing        |
| duration_ms    | integer       | round-trip time of the API call        |
| stop_reason    | text          | e.g. end_turn, max_tokens, refusal     |
| reasoning      | text          | reasoning level used, or null if n/a   |

### Why the prompt is stored in pieces

`prompt` holds the raw question and nothing else. The Settings context block
lives in `context`, the Brief button's prefix is recorded as the `brief` flag,
and `compose_prompt()` joins all three only on the way to the provider.

This matters because the context block routinely carries personal data (the
default Settings include location, and the free-form field is meant for things
like height and weight). Anything that treats the prompt as publishable - the
History preview, and public share links - reads `prompt` and can never leak the
rest by accident.

`prompt_is_raw` is the discriminator. Rows written before the split hold the
whole composed blob in `prompt` and cannot be reliably taken apart again, so
features that assume a raw prompt must check this flag first. A null `context`
is *not* a substitute test: it also describes a new row written with every
Settings toggle switched off.

Schema changes are applied on every boot by the `MIGRATIONS` tuple in
`backend/app.py` (`ADD COLUMN IF NOT EXISTS`), so a `docker compose up` is the
whole migration story.

### Attachments

Two more tables hold the images. They live in Postgres rather than on a volume
so `db_data` stays the single thing to back up, and so a row can never outlive
its bytes or vice versa.

`images` - content-addressed, one row per distinct file:

| column           | type        | notes                                       |
|------------------|-------------|---------------------------------------------|
| sha256           | text PK     | storage key; equal bytes are stored once    |
| media_type       | text        | one of `ALLOWED_IMAGE_TYPES`                |
| bytes            | bytea       | the original upload                         |
| byte_size        | integer     |                                             |
| width / height   | integer     | client-reported, layout hints only          |
| thumb            | bytea       | 512px WebP from the browser, or null        |
| thumb_media_type | text        |                                             |
| created_at       | timestamptz |                                             |

`prompt_images` - the join, `(prompt_id, position)` as the primary key so
attachments come back in the order they were sent. `prompt_id` is
`ON DELETE CASCADE`, which is what makes "delete it from history and it's gone"
true. `image_sha` is `ON DELETE RESTRICT`.

**Thumbnails are made in the browser** (`makeThumbnail()` in `app.jsx`: canvas →
WebP at 512px). That keeps the server from ever decoding untrusted image data,
and means History pulls tens of kilobytes per attachment instead of megabytes.
A thumbnail is optional: small images skip it and serve their original in its
place, as do images uploaded before thumbnails existed.

**Orphan sweep.** Deleting a prompt cascades its join rows but not the bytes -
deduplication means the image may still belong to another prompt. So
`delete_orphan_images()` runs right after the delete and drops only rows nothing
references any more. There is no background garbage collector to forget about.

## HTTP API

- `POST /api/login` `{pin}` → `{ok}` / `401` / `429 {retry_after}`
- `GET  /api/session` → `{authenticated}`
- `POST /api/logout` → `{ok}`
- `GET  /api/config` → `{models:[{id,label,provider,provider_label,input,output,context_window,depth,est_pln}], default, usd_pln, max_tokens, max_context_chars, image_limits:{max_images,max_image_mb,max_total_mb,allowed_types}}` *(auth)*
- `POST /api/generate` `{model, prompt, context?, brief?, depth, images?}` → answer + tokens + cost + duration *(auth)*.
  `prompt` is the raw question; `context` is the Settings block (capped at
  `max_context_chars`); `brief` asks for the "very brief: " prefix. The server joins them
  with `compose_prompt()` and stores the three separately - see the data model above.
  `images` is an optional array of `{data (base64, no data: prefix), media_type, width?,
  height?, thumb?, thumb_media_type?}`, validated server-side against `image_limits`
  before any provider is called, then stored (see Attachments above).
- `GET  /api/history?q=&before=` → page of rows (preview + attachment descriptors, 30 per page, newest first); `q`
  filters prompt+answer (ILIKE), `before` is an id cursor for infinite scroll *(auth)*
- `GET  /api/history/:id` → full row, plus `images:[{sha,media_type,byte_size,width,height,has_thumb}]` *(auth)*
- `DELETE /api/history/:id` → delete a row, its join rows, and any image left unreferenced *(auth)*
- `GET  /api/images/:sha` → the original bytes *(auth)*
- `GET  /api/images/:sha/thumb` → the thumbnail, falling back to the original when there is none *(auth)*
- `GET  /api/health` → `{ok}`

## Models & pricing

Three providers, selected per model via a `provider` field. Pricing is USD per 1M
tokens (input / output). `context_window` (tokens) is shown in the Ask view's
session sidebar.

**`MODELS` is a catalog; `MODEL_ORDER` is the menu.** They are deliberately separate.
`prompts.model` stores whichever model actually served an answer, so a model that
leaves the picker must keep its label and its rate forever - otherwise old history
renders as a raw id and a fallback-served response gets mis-priced. Retiring a model
means removing it from `MODEL_ORDER` only. `/api/generate` accepts ids from
`MODEL_ORDER`, not from the whole catalog.

Selectable (`MODEL_ORDER`, in dropdown order):

| id                     | label                 | provider  | input | output | context_window |
|------------------------|-----------------------|-----------|-------|--------|----------------|
| gemini-3.1-flash-lite  | Gemini 3.1 Flash Lite | google    | 0.25  | 1.50   | 1M             |
| gemini-3.8-flash       | Gemini 3.8 Flash      | google    | 1.50  | 7.50   | 1M             |
| claude-haiku-4-5       | Haiku 4.5             | anthropic | 1     | 5      | 200k           |
| claude-fable-5         | Fable 5               | anthropic | 10    | 50     | 1M             |
| gpt-5.4-nano           | GPT-5.4 Nano          | openai    | 0.20  | 1.25   | 400k           |
| gpt-6-astra            | GPT-6 Astra           | openai    | 10    | 50     | 400k           |

Retired - catalog-only, for history labels and pricing:

| id                     | label                 | provider  | input | output |
|------------------------|-----------------------|-----------|-------|--------|
| gemini-2.5-flash-lite  | Gemini 2.5 Flash Lite | google    | 0.10  | 0.40   |
| gemini-3.5-flash       | Gemini 3.5 Flash      | google    | 1.50  | 9.00   |
| claude-sonnet-4-6      | Sonnet 4.6            | anthropic | 3     | 15     |
| claude-opus-4-8        | Opus 4.8              | anthropic | 5     | 25     |

Notes:
- Default model is **`gemini-3.1-flash-lite`**.
- Gemini 3.8 Flash is listed at its **standard** rate (1.50 / 7.50), not the
  introductory 0.75 / 3.75 running until 2026-12-31 - a deliberate choice so
  estimates never understate what it will cost from January.
- The UI groups the model dropdown by provider (Anthropic / Google Gemini / OpenAI)
  and shows a per-prompt price estimate next to each model.
- **Price display is in PLN.** Cost is computed in USD from real token counts, then
  the frontend multiplies by `USD_TO_PLN` (constant in `backend/app.py`, currently
  `4.0`) for display. The DB stores `cost_usd` (USD stays the source of truth).
- The dropdown estimate assumes a typical prompt of `ESTIMATE_INPUT_TOKENS` (1000)
  + `ESTIMATE_OUTPUT_TOKENS` (1500) tokens; `/api/config` returns the pre-computed
  `est_pln` per model plus the `usd_pln` rate.
- Fable 5 always thinks and can be slow; it is called through the beta endpoint
  with `fallbacks: "default"`, which routes by refusal category server-side - so
  there is no fallback model list to keep in sync with `MODEL_ORDER`. The served
  model (which may be the fallback) is what gets priced and stored, which is the
  other reason retired ids stay in the catalog.
- Anthropic refusals (`stop_reason == "refusal"`) and empty/blocked Gemini
  responses are surfaced as a short note.

## Reasoning controls (per model)

The UI exposes a single two-state depth switch, **low** / **max**. `/api/config`
returns `depth: {available}` per model; the switch is shown only when it's `true`,
and the default is always `low`. `/api/generate` takes `depth`.

Each model's `reasoning` key maps those two states onto its own provider params.
A model with no `reasoning` key has no adjustable reasoning at all:

- **Fable 5**: `low` → `output_config.effort: "low"`, `max` → `"max"`; thinking is
  always on (`thinking: {type: "adaptive"}`), because Fable rejects any other setting.
- **Haiku 4.5**: no `reasoning` key - the switch is hidden and nothing is sent.
- **Gemini 3.x**: `low`/`max` → `thinking_config.thinking_level` of `low`/`high`,
  built defensively (the field is skipped if the SDK doesn't accept it).
- **OpenAI**: `low`/`max` → `reasoning_effort` of `low`/`xhigh`. Chat Completions
  caps these models at `xhigh`; the Responses API's `max` is not accepted there.

The applied depth is summarised into the `reasoning` column (`Low` / `Max`) and
shown in history.

## Prompt context (client-side settings)

The Settings tab stores a small object in the browser (`localStorage`, key
`briefai_settings`). `buildContext()` turns it into a plain text block, which the
frontend sends as the separate `context` field - it is never glued onto the prompt
client-side.

- `now: <date>, <HH:MM>` - single "Date & time" toggle (default on), from the browser clock.
- `user is currently in <text>` - editable location (default "Bielsko-Biała").
- `user data: <text>` - free-form personal context (default off).

Composition happens server-side in `compose_prompt()`: `<context>\n\n` + optional
`very brief: ` (Brief button) + the prompt. Each part is independently toggleable;
all off → just the raw prompt. The preferences themselves stay in the browser; only
the rendered block travels, and it is capped at `MAX_CONTEXT_CHARS` (8000).

## Live cost estimate

Shown next to the input as `est_in: X zł`, priced from the model's per-1M input
rate × `USD_TO_PLN`. **Input only** - the output side isn't estimated, since it
can't be predicted before the model answers (the exact total cost, input + output,
is shown after the answer). Input tokens ≈ `estimateChars()` / 4 - the prompt plus
what the context block and Brief prefix will add once the server joins them - plus, for any
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
- Persisted, content-addressed, with a browser-made thumbnail alongside - see
  **Attachments** under the data model. History shows them; deleting a prompt
  takes its images with it unless another prompt still uses the same file.
- Limits are per-request: the 24MB total covers the originals, and thumbnails
  have their own `MAX_TOTAL_THUMB_BYTES` ceiling on top. Both sit under nginx's
  `client_max_body_size`, which has to allow for base64's ~33% overhead.

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
