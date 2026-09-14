import base64
import binascii
import hashlib
import os
import re
import secrets
import threading
import time
import traceback
from datetime import timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
import pyotp
from anthropic import Anthropic
from flask import Flask, jsonify, request, session
from google import genai
from google.genai import types as genai_types
from openai import OpenAI

TOTP_SECRET = os.environ.get("TOTP_SECRET", "")
if not re.fullmatch(r"[A-Z2-7]+=*", TOTP_SECRET):
    raise RuntimeError(
        "TOTP_SECRET must be a base32 secret (set it in .env - "
        "run backend/generate_totp_secret.py locally to create one)"
    )
totp = pyotp.TOTP(TOTP_SECRET)

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print("WARNING: SECRET_KEY is not set; using a temporary key. "
          "Sessions will reset on restart.")

DATABASE_URL = os.environ["DATABASE_URL"]
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"

anthropic_client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

SYSTEM_PROMPT = (
    "You are Brief AI. Answer the user clearly and directly. "
    "Use Markdown for structure and code when it helps. "
    "Be concise: no preamble, no filler, just the answer."
)

# Catalog of every model this instance has ever served. Entries are never
# removed: `prompts.model` stores the id that actually served each answer, so
# dropping one would leave old history labelled with a raw id and would strip
# the pricing needed to value a fallback-served response. Retiring a model
# means taking it out of MODEL_ORDER below, not out of here.
#
# provider: "anthropic" | "google" | "openai"; input/output priced in USD per
# 1M tokens. "reasoning" (optional) maps the two UI depth states, "low" and
# "max", to the provider-specific params that give this model its lowest and
# highest reasoning effort. Models without a "reasoning" key have no depth
# control at all - they don't support adjustable reasoning.
MODELS = {
    "gemini-3.1-flash-lite": {
        "label": "Gemini 3.1 Flash Lite", "provider": "google", "input": 0.25, "output": 1.50, "context_window": 1_000_000,
        "reasoning": {"low": {"effort": "low"}, "max": {"effort": "high"}},
    },
    "gemini-3.8-flash": {
        # Google is running an introductory 0.75 / 3.75 until 2026-12-31; the
        # standard rate below is deliberately used instead, so estimates never
        # understate what this will cost from January.
        "label": "Gemini 3.8 Flash", "provider": "google", "input": 1.50, "output": 7.50, "context_window": 1_000_000,
        "reasoning": {"low": {"effort": "low"}, "max": {"effort": "high"}},
    },
    "claude-haiku-4-5": {"label": "Haiku 4.5", "provider": "anthropic", "input": 1.0, "output": 5.0, "context_window": 200_000},
    "claude-fable-5": {
        "label": "Fable 5", "provider": "anthropic", "input": 10.0, "output": 50.0, "context_window": 1_000_000,
        "reasoning": {"low": {"effort": "low", "thinking": True}, "max": {"effort": "max", "thinking": True}},
    },
    "gpt-5.4-nano": {
        "label": "GPT-5.4 Nano", "provider": "openai", "input": 0.20, "output": 1.25, "context_window": 400_000,
        "reasoning": {"low": {"effort": "low"}, "max": {"effort": "xhigh"}},
    },
    "gpt-6-astra": {
        "label": "GPT-6 Astra", "provider": "openai", "input": 10.0, "output": 50.0, "context_window": 400_000,
        # Chat Completions caps reasoning_effort at "xhigh" for this model
        # (the Responses API's "max" isn't accepted here).
        "reasoning": {"low": {"effort": "low"}, "max": {"effort": "xhigh"}},
    },

    # Retired - kept for history labels, pricing, and fallback lookups only.
    "gemini-2.5-flash-lite": {"label": "Gemini 2.5 Flash Lite", "provider": "google", "input": 0.10, "output": 0.40},
    "gemini-3.5-flash": {"label": "Gemini 3.5 Flash", "provider": "google", "input": 1.50, "output": 9.00},
    "claude-sonnet-4-6": {"label": "Sonnet 4.6", "provider": "anthropic", "input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"label": "Opus 4.8", "provider": "anthropic", "input": 5.0, "output": 25.0},
}

# The models offered in the UI, in dropdown order - a subset of MODELS, and the
# only ids /api/generate accepts.
MODEL_ORDER = [
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "claude-haiku-4-5",
    "claude-fable-5",
    "gpt-5.4-nano",
    "gpt-6-astra",
]
SELECTABLE_MODELS = set(MODEL_ORDER)
PROVIDER_LABELS = {"anthropic": "Anthropic", "google": "Google Gemini", "openai": "OpenAI"}
DEFAULT_MODEL = "gemini-3.1-flash-lite"

MAX_TOKENS = 16384
LOGIN_COOLDOWN = 3.0
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 5 * 60

# The two reasoning-depth states exposed in the UI. Each reasoning-capable
# model maps these to its own provider-specific effort/thinking params.
DEPTH_LEVELS = ["low", "max"]
DEFAULT_DEPTH = "low"

# Per-prompt price estimate assumptions, converted to PLN with a fixed rate.
ESTIMATE_INPUT_TOKENS = 1000
ESTIMATE_OUTPUT_TOKENS = 1500
USD_TO_PLN = 4.0

# Image attachment limits, enforced here (never trust the client) and mirrored
# to the frontend via /api/config so there's one source of truth for the numbers.
MAX_IMAGES = 20
MAX_IMAGE_MB = 10
MAX_TOTAL_IMAGE_MB = 24
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# Thumbnails are produced in the browser (canvas -> WebP) and uploaded next to
# the original, so the server never decodes untrusted image data. The cap is
# generous for a 512px WebP; anything near it means the client sent something
# other than a thumbnail.
MAX_THUMB_BYTES = 600_000
# Thumbnails ride along in the same request as the originals, so they need an
# aggregate ceiling too - otherwise a client could add MAX_IMAGES * 600KB on
# top of the 24MB budget and blow past nginx's body limit.
MAX_TOTAL_THUMB_BYTES = 4_000_000
ALLOWED_THUMB_TYPES = {"image/webp", "image/jpeg", "image/png"}

# Sanity bounds on the client-reported pixel dimensions. They are only used for
# layout hints and the token estimate, never for decoding, so loose is fine.
MAX_IMAGE_DIMENSION = 100_000

# The Settings context block is user-controlled free text arriving from the
# browser, so it needs an upper bound. Generous enough that nobody hits it by
# writing about themselves; small enough that it can't be used as a payload.
MAX_CONTEXT_CHARS = 8000

# Prefix the Brief button asks for. Applied when composing the text sent to the
# model; never stored in the `prompt` column (the `brief` flag records it).
BRIEF_PREFIX = "very brief: "

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS prompts (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    answer TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    reasoning TEXT,
    context TEXT,
    brief BOOLEAN NOT NULL DEFAULT FALSE,
    prompt_is_raw BOOLEAN NOT NULL DEFAULT FALSE
)
"""

# Attachments live in Postgres alongside the prompts, so `db_data` stays the
# single thing to back up and a row can never outlive or outlast its bytes.
#
# Images are content-addressed by sha256, which makes deduplication free: the
# same screenshot attached to five prompts is stored once. `prompt_images` is
# the join, ordered by `position` so attachments come back in the order they
# were sent.
IMAGES_SQL = """
CREATE TABLE IF NOT EXISTS images (
    sha256 TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    bytes BYTEA NOT NULL,
    byte_size INTEGER NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    thumb BYTEA,
    thumb_media_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# ON DELETE CASCADE on prompt_id is what makes "delete it from history and it's
# gone" true. The image rows themselves are swept separately once nothing
# references them (see delete_orphan_images) - cascade can't do that job,
# because deduplication means one image may still belong to another prompt.
PROMPT_IMAGES_SQL = """
CREATE TABLE IF NOT EXISTS prompt_images (
    prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    image_sha TEXT NOT NULL REFERENCES images(sha256) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    PRIMARY KEY (prompt_id, position)
)
"""

PROMPT_IMAGES_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS prompt_images_image_sha_idx "
    "ON prompt_images (image_sha)"
)

# Columns added after the table shipped, applied on every boot. `prompt_is_raw`
# is the discriminator that matters: rows written before the split have the
# whole composed blob - context lines, brief prefix and all - sitting in
# `prompt`, and there is no reliable way to take it apart again. Anything that
# treats `prompt` as "what the user actually typed" (sharing, previews) must
# check this flag first, because a NULL `context` is ambiguous: it also
# describes a new row written with every Settings toggle switched off.
MIGRATIONS = (
    "ALTER TABLE prompts ADD COLUMN IF NOT EXISTS reasoning TEXT",
    "ALTER TABLE prompts ADD COLUMN IF NOT EXISTS context TEXT",
    "ALTER TABLE prompts ADD COLUMN IF NOT EXISTS brief BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE prompts ADD COLUMN IF NOT EXISTS prompt_is_raw BOOLEAN NOT NULL DEFAULT FALSE",
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

_login_lock = threading.Lock()
_last_attempt = {}
_login_failures = {}  # ip -> (count, locked_until_monotonic)
_last_used_step = None  # last accepted TOTP time-step, to reject replay


def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = None
    for _ in range(30):
        try:
            conn = get_db()
            break
        except psycopg2.OperationalError:
            time.sleep(1)
    if conn is None:
        raise RuntimeError("Could not connect to the database")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute(IMAGES_SQL)
            cur.execute(PROMPT_IMAGES_SQL)
            cur.execute(PROMPT_IMAGES_INDEX_SQL)
            for statement in MIGRATIONS:
                cur.execute(statement)
    except psycopg2.Error as exc:
        print("init_db:", exc)
    finally:
        conn.close()


def estimate_pln(cfg):
    usd = (
        ESTIMATE_INPUT_TOKENS / 1_000_000 * cfg["input"]
        + ESTIMATE_OUTPUT_TOKENS / 1_000_000 * cfg["output"]
    )
    return round(usd * USD_TO_PLN, 4)


def build_reasoning_label(model, depth):
    if not MODELS[model].get("reasoning"):
        return None
    return depth.capitalize() if depth in DEPTH_LEVELS else None


def compose_prompt(prompt, context, brief):
    """Build the text actually sent to the model.

    The three parts are stored separately and only ever joined here, on the way
    out. That keeps `prompt` holding nothing but what the user typed, which is
    what History previews and what a public share may show - the Settings
    context (date, location, personal data) must never travel with it.
    """
    parts = []
    if context:
        parts.append(context.strip() + "\n\n")
    if brief:
        parts.append(BRIEF_PREFIX)
    parts.append(prompt)
    return "".join(parts)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _clamp_dimension(value):
    """Pixel dimensions are client-reported layout hints, not trusted input -
    anything unusable becomes 0 rather than an error."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= MAX_IMAGE_DIMENSION else 0


def validate_thumb(img, index):
    """Validate the optional browser-generated thumbnail on one image entry.

    Returns (None, (raw_bytes, media_type)) when present and valid,
    (None, (None, None)) when absent, or ((code, message), None) on a
    violation. A missing thumbnail is fine: the original is served instead.
    """
    data = img.get("thumb")
    if not data:
        return None, (None, None)
    if not isinstance(data, str):
        return ("invalid_images", f"image {index} has a non-string thumbnail"), None

    media_type = img.get("thumb_media_type")
    if media_type not in ALLOWED_THUMB_TYPES:
        return (
            "unsupported_image_type",
            f"image {index} has an unsupported thumbnail type {media_type!r}.",
        ), None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return ("invalid_images", f"image {index} has a thumbnail that is not valid base64"), None
    if len(raw) > MAX_THUMB_BYTES:
        return (
            "image_too_large",
            f"image {index} has a {len(raw) / 1000:.0f}KB thumbnail, "
            f"max is {MAX_THUMB_BYTES // 1000}KB.",
        ), None
    return None, (raw, media_type)


def validate_images(images):
    """Decode and validate an `images` payload against MAX_IMAGES /
    MAX_IMAGE_MB / MAX_TOTAL_IMAGE_MB / ALLOWED_IMAGE_TYPES.

    Returns (None, decoded_images) on success, where each entry carries the
    original "data" plus the decoded "raw" bytes, its "sha256" (the storage
    key - equal bytes are stored once), the client-reported "width"/"height",
    and an optional decoded "thumb"/"thumb_media_type". Returns
    ((error_code, message), None) on the first violation found.
    """
    if not images:
        return None, []
    if not isinstance(images, list):
        return ("invalid_images", "images must be a list"), None
    if len(images) > MAX_IMAGES:
        return (
            "too_many_images",
            f"Max {MAX_IMAGES} images per prompt (got {len(images)}).",
        ), None

    decoded = []
    total_bytes = 0
    total_thumb_bytes = 0
    max_image_bytes = MAX_IMAGE_MB * 1_000_000
    max_total_bytes = MAX_TOTAL_IMAGE_MB * 1_000_000
    for i, img in enumerate(images):
        if not isinstance(img, dict):
            return ("invalid_images", f"image {i} is not an object"), None
        media_type = img.get("media_type")
        data = img.get("data")
        if media_type not in ALLOWED_IMAGE_TYPES:
            return (
                "unsupported_image_type",
                f"image {i} has unsupported type {media_type!r}.",
            ), None
        if not isinstance(data, str) or not data:
            return ("invalid_images", f"image {i} is missing base64 data"), None
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return ("invalid_images", f"image {i} is not valid base64"), None

        if len(raw) > max_image_bytes:
            return (
                "image_too_large",
                f"image {i} is {len(raw) / 1_000_000:.1f}MB, max is {MAX_IMAGE_MB}MB.",
            ), None
        total_bytes += len(raw)
        if total_bytes > max_total_bytes:
            return (
                "images_too_large_total",
                f"Attached images total {total_bytes / 1_000_000:.1f}MB, "
                f"max is {MAX_TOTAL_IMAGE_MB}MB.",
            ), None

        error, thumb = validate_thumb(img, i)
        if error:
            return error, None
        thumb_raw, thumb_type = thumb
        if thumb_raw:
            total_thumb_bytes += len(thumb_raw)
            if total_thumb_bytes > MAX_TOTAL_THUMB_BYTES:
                return (
                    "images_too_large_total",
                    f"Thumbnails total {total_thumb_bytes / 1_000_000:.1f}MB, "
                    f"max is {MAX_TOTAL_THUMB_BYTES // 1_000_000}MB.",
                ), None

        decoded.append({
            "media_type": media_type,
            "data": data,
            "raw": raw,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "width": _clamp_dimension(img.get("width")),
            "height": _clamp_dimension(img.get("height")),
            "thumb": thumb_raw,
            "thumb_media_type": thumb_type,
        })

    return None, decoded


def store_images(cur, prompt_id, images):
    """Persist the attachments of one prompt.

    Content addressing makes the image insert idempotent: ON CONFLICT DO
    NOTHING means re-attaching the same file costs a join row and nothing more.
    The thumbnail is backfilled if this upload brought one and the stored copy
    has none (an image first seen through an older client).
    """
    for position, img in enumerate(images):
        cur.execute(
            """INSERT INTO images
                   (sha256, media_type, bytes, byte_size, width, height, thumb, thumb_media_type)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (sha256) DO UPDATE
                   SET thumb = COALESCE(images.thumb, EXCLUDED.thumb),
                       thumb_media_type = COALESCE(images.thumb_media_type, EXCLUDED.thumb_media_type)""",
            (img["sha256"], img["media_type"], psycopg2.Binary(img["raw"]), len(img["raw"]),
             img["width"], img["height"],
             psycopg2.Binary(img["thumb"]) if img["thumb"] else None,
             img["thumb_media_type"]),
        )
        cur.execute(
            "INSERT INTO prompt_images (prompt_id, image_sha, position) VALUES (%s, %s, %s)",
            (prompt_id, img["sha256"], position),
        )


def delete_orphan_images(cur, shas):
    """Drop image rows that no prompt points at any more.

    Deleting a prompt cascades its `prompt_images` rows, but not the bytes:
    deduplication means the same image may still belong to another prompt.
    This runs right after a delete rather than as a background sweep, so there
    is no separate garbage collector to forget about.
    """
    if not shas:
        return
    cur.execute(
        """DELETE FROM images
           WHERE sha256 = ANY(%s)
             AND NOT EXISTS (
                 SELECT 1 FROM prompt_images WHERE image_sha = images.sha256
             )""",
        (list(shas),),
    )


def fetch_prompt_images(cur, prompt_ids):
    """Attachment metadata (never the bytes) for a batch of prompts.

    Returns {prompt_id: [descriptor, ...]} in attachment order. Batched on
    purpose - the History list renders thirty rows at a time and must not turn
    that into thirty queries.
    """
    if not prompt_ids:
        return {}
    cur.execute(
        """SELECT pi.prompt_id, pi.image_sha, pi.position,
                  i.media_type, i.byte_size, i.width, i.height,
                  (i.thumb IS NOT NULL) AS has_thumb
           FROM prompt_images pi
           JOIN images i ON i.sha256 = pi.image_sha
           WHERE pi.prompt_id = ANY(%s)
           ORDER BY pi.prompt_id, pi.position""",
        (list(prompt_ids),),
    )
    by_prompt = {}
    for row in cur.fetchall():
        by_prompt.setdefault(row["prompt_id"], []).append({
            "sha": row["image_sha"],
            "media_type": row["media_type"],
            "byte_size": row["byte_size"],
            "width": row["width"],
            "height": row["height"],
            "has_thumb": row["has_thumb"],
        })
    return by_prompt


def require_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)
    return wrapper


def call_anthropic(model, prompt, depth, images=None):
    cfg = MODELS[model]
    content = []
    for img in images or []:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]},
        })
    content.append({"type": "text", "text": prompt})
    kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    reasoning = cfg.get("reasoning", {}).get(depth)
    if reasoning:
        kwargs["output_config"] = {"effort": reasoning["effort"]}
        if reasoning.get("thinking"):
            kwargs["thinking"] = {"type": "adaptive"}

    if model == "claude-fable-5":
        try:
            # "default" routes by refusal category server-side, so there is no
            # fallback model list to keep in sync with MODEL_ORDER.
            resp = anthropic_client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            )
        except TypeError:
            resp = anthropic_client.messages.create(**kwargs)
    else:
        resp = anthropic_client.messages.create(**kwargs)

    if resp.stop_reason == "refusal":
        answer = "_The model declined to answer this request._"
    else:
        answer = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip() or "_No response._"

    return {
        "answer": answer,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
        "served": getattr(resp, "model", model),
    }




def call_gemini(model, prompt, depth, images=None):
    if gemini_client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    config_args = dict(system_instruction=SYSTEM_PROMPT, max_output_tokens=MAX_TOKENS)
    reasoning = MODELS[model].get("reasoning", {}).get(depth)
    if reasoning:
        try:
            config_args["thinking_config"] = genai_types.ThinkingConfig(thinking_level=reasoning["effort"])
        except Exception:
            pass

    contents = [
        genai_types.Part.from_bytes(data=img["raw"], mime_type=img["media_type"])
        for img in images or []
    ]
    contents.append(prompt)

    resp = gemini_client.models.generate_content(
        model=model,
        contents=contents,
        config=genai_types.GenerateContentConfig(**config_args),
    )

    try:
        answer = (resp.text or "").strip()
    except Exception:
        answer = ""
    if not answer:
        answer = "_The model returned no content._"

    usage = resp.usage_metadata
    in_tok = getattr(usage, "prompt_token_count", 0) or 0
    out_tok = (getattr(usage, "candidates_token_count", 0) or 0) + \
              (getattr(usage, "thoughts_token_count", 0) or 0)

    stop_reason = None
    try:
        stop_reason = str(resp.candidates[0].finish_reason)
    except Exception:
        pass

    return {
        "answer": answer,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "stop_reason": stop_reason,
        "served": model,
    }


def call_openai(model, prompt, depth, images=None):
    if openai_client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}}
        for img in images or []
    ]
    user_content.append({"type": "text", "text": prompt})

    kwargs = dict(
        model=model,
        max_completion_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    reasoning = MODELS[model].get("reasoning", {}).get(depth)
    if reasoning:
        kwargs["reasoning_effort"] = reasoning["effort"]

    resp = openai_client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    answer = (choice.message.content or "").strip() or "_No response._"

    return {
        "answer": answer,
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        "stop_reason": choice.finish_reason,
        "served": resp.model,
    }


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.post("/api/login")
def login():
    global _last_used_step
    ip = client_ip()
    now = time.monotonic()
    with _login_lock:
        count, locked_until = _login_failures.get(ip, (0, 0))
        if now < locked_until:
            return jsonify(error="too_many_attempts", retry_after=round(locked_until - now, 1)), 429

        wait = LOGIN_COOLDOWN - (now - _last_attempt.get(ip, 0))
        if wait > 0:
            return jsonify(error="too_many_attempts", retry_after=round(wait, 1)), 429
        _last_attempt[ip] = now

    data = request.get_json(silent=True) or {}
    code = str(data.get("code", ""))
    step = int(time.time()) // 30
    valid = (
        re.fullmatch(r"\d{6}", code)
        and totp.verify(code, valid_window=1)
        and step != _last_used_step
    )

    with _login_lock:
        if valid:
            _login_failures.pop(ip, None)
            _last_used_step = step
        else:
            count, _ = _login_failures.get(ip, (0, 0))
            count += 1
            locked_until = now + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_FAILURES else 0
            _login_failures[ip] = (count, locked_until)

    if valid:
        session.permanent = True
        session["auth"] = True
        return jsonify(ok=True)
    return jsonify(error="invalid_code"), 401


@app.get("/api/session")
def check_session():
    return jsonify(authenticated=bool(session.get("auth")))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/config")
@require_auth
def config():
    models = []
    for model_id in MODEL_ORDER:
        cfg = MODELS[model_id]
        models.append({
            "id": model_id,
            "label": cfg["label"],
            "provider": cfg["provider"],
            "provider_label": PROVIDER_LABELS[cfg["provider"]],
            "input": cfg["input"],
            "output": cfg["output"],
            "context_window": cfg["context_window"],
            "depth": {"available": bool(cfg.get("reasoning"))},
            "est_pln": estimate_pln(cfg),
        })
    return jsonify(
        models=models,
        default=DEFAULT_MODEL,
        usd_pln=USD_TO_PLN,
        max_tokens=MAX_TOKENS,
        max_context_chars=MAX_CONTEXT_CHARS,
        image_limits={
            "max_images": MAX_IMAGES,
            "max_image_mb": MAX_IMAGE_MB,
            "max_total_mb": MAX_TOTAL_IMAGE_MB,
            "allowed_types": sorted(ALLOWED_IMAGE_TYPES),
        },
    )


@app.post("/api/generate")
@require_auth
def generate():
    data = request.get_json(silent=True) or {}
    model = data.get("model", DEFAULT_MODEL)
    prompt = (data.get("prompt") or "").strip()
    if model not in SELECTABLE_MODELS:
        return jsonify(error="unknown_model"), 400
    if not prompt:
        return jsonify(error="empty_prompt"), 400

    context = (data.get("context") or "").strip()
    if len(context) > MAX_CONTEXT_CHARS:
        return jsonify(
            error="context_too_long",
            detail=f"Prompt context is {len(context)} characters, max is {MAX_CONTEXT_CHARS}.",
        ), 400
    brief = bool(data.get("brief"))

    depth = data.get("depth", DEFAULT_DEPTH)
    if depth not in DEPTH_LEVELS:
        depth = DEFAULT_DEPTH

    error, images = validate_images(data.get("images"))
    if error:
        code, message = error
        return jsonify(error=code, detail=message), 400

    composed = compose_prompt(prompt, context, brief)

    provider = MODELS[model]["provider"]
    start = time.perf_counter()
    try:
        if provider == "google":
            result = call_gemini(model, composed, depth, images)
        elif provider == "openai":
            result = call_openai(model, composed, depth, images)
        else:
            result = call_anthropic(model, composed, depth, images)
    except Exception as exc:  # surface API/network errors to the client
        traceback.print_exc()
        return jsonify(error="api_error", detail=str(exc)), 502
    duration_ms = int((time.perf_counter() - start) * 1000)

    served = result["served"]
    price = MODELS.get(served, MODELS[model])
    in_tok = result["input_tokens"]
    out_tok = result["output_tokens"]
    cost = in_tok / 1_000_000 * price["input"] + out_tok / 1_000_000 * price["output"]

    stored_reasoning = build_reasoning_label(model, depth)

    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO prompts
               (model, prompt, context, brief, prompt_is_raw, answer,
                input_tokens, output_tokens, cost_usd, duration_ms, stop_reason, reasoning)
               VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, created_at""",
            (served, prompt, context or None, brief, result["answer"],
             in_tok, out_tok, cost, duration_ms,
             result["stop_reason"], stored_reasoning),
        )
        row = cur.fetchone()
        store_images(cur, row[0], images)
    conn.close()

    return jsonify(
        id=row[0],
        created_at=row[1].isoformat(),
        model=served,
        model_label=MODELS.get(served, {}).get("label", served),
        answer=result["answer"],
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        duration_ms=duration_ms,
        stop_reason=result["stop_reason"],
        reasoning=stored_reasoning,
    )


HISTORY_PAGE = 30


@app.get("/api/history")
@require_auth
def history():
    query = (request.args.get("q") or "").strip()
    before = request.args.get("before")

    conditions = []
    params = []
    if query:
        like = "%" + query + "%"
        conditions.append("(prompt ILIKE %s OR answer ILIKE %s)")
        params.extend([like, like])
    if before:
        try:
            conditions.append("id < %s")
            params.append(int(before))
        except ValueError:
            pass
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""SELECT id, created_at, model, LEFT(prompt, 140) AS prompt_preview,
                       input_tokens, output_tokens, cost_usd, duration_ms, reasoning
                FROM prompts
                {where}
                ORDER BY id DESC
                LIMIT {HISTORY_PAGE}""",
            params,
        )
        rows = cur.fetchall()
        images_by_prompt = fetch_prompt_images(cur, [r["id"] for r in rows])
    conn.close()

    items = [
        {
            "id": r["id"],
            "created_at": r["created_at"].isoformat(),
            "model": r["model"],
            "model_label": MODELS.get(r["model"], {}).get("label", r["model"]),
            "prompt_preview": r["prompt_preview"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cost_usd": r["cost_usd"],
            "duration_ms": r["duration_ms"],
            "reasoning": r["reasoning"],
            "images": images_by_prompt.get(r["id"], []),
        }
        for r in rows
    ]
    return jsonify(items=items)


@app.get("/api/history/<int:item_id>")
@require_auth
def history_item(item_id):
    with get_db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM prompts WHERE id = %s", (item_id,))
        row = cur.fetchone()
        images = fetch_prompt_images(cur, [item_id]).get(item_id, []) if row else []
    conn.close()
    if not row:
        return jsonify(error="not_found"), 404
    row["created_at"] = row["created_at"].isoformat()
    row["model_label"] = MODELS.get(row["model"], {}).get("label", row["model"])
    row["images"] = images
    return jsonify(row)


@app.delete("/api/history/<int:item_id>")
@require_auth
def delete_history_item(item_id):
    with get_db() as conn, conn.cursor() as cur:
        # Read the attachments before the row goes, because the cascade takes
        # the join rows with it and there would be nothing left to look up.
        cur.execute("SELECT image_sha FROM prompt_images WHERE prompt_id = %s", (item_id,))
        shas = {r[0] for r in cur.fetchall()}
        cur.execute("DELETE FROM prompts WHERE id = %s", (item_id,))
        delete_orphan_images(cur, shas)
    conn.close()
    return jsonify(ok=True)


IMAGE_SHA_RE = re.compile(r"[0-9a-f]{64}")
# Content-addressed, so the bytes behind a sha can never change. `private`
# keeps it out of shared caches while the route is behind auth.
IMAGE_CACHE_CONTROL = "private, max-age=31536000, immutable"


def _serve_image(sha, want_thumb):
    if not IMAGE_SHA_RE.fullmatch(sha or ""):
        return jsonify(error="not_found"), 404

    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT bytes, media_type, thumb, thumb_media_type FROM images WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify(error="not_found"), 404

    data, media_type, thumb, thumb_media_type = row
    # An image uploaded before thumbnails existed, or one already small enough
    # that the browser skipped making one, serves its original here.
    if want_thumb and thumb is not None:
        data, media_type = thumb, thumb_media_type

    resp = app.response_class(bytes(data), mimetype=media_type)
    resp.headers["Cache-Control"] = IMAGE_CACHE_CONTROL
    resp.headers["ETag"] = f'"{sha}{"-t" if want_thumb else ""}"'
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/api/images/<sha>")
@require_auth
def image_full(sha):
    return _serve_image(sha, want_thumb=False)


@app.get("/api/images/<sha>/thumb")
@require_auth
def image_thumb(sha):
    return _serve_image(sha, want_thumb=True)


init_db()
