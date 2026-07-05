import hmac
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
from anthropic import Anthropic
from flask import Flask, jsonify, request, session
from google import genai
from google.genai import types as genai_types

APP_PIN = os.environ.get("APP_PIN", "")
if not re.fullmatch(r"\d{6}", APP_PIN):
    raise RuntimeError("APP_PIN must be a 6-digit number (set it in .env)")

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

SYSTEM_PROMPT = (
    "You are Brief AI. Answer the user clearly and directly. "
    "Use Markdown for structure and code when it helps. "
    "Be concise: no preamble, no filler, just the answer."
)

# provider: "anthropic" | "google"; input/output priced in USD per 1M tokens;
# effort applies only to Anthropic models that accept output_config.effort.
MODELS = {
    "gemini-2.5-flash-lite": {"label": "Gemini 2.5 Flash Lite", "provider": "google", "input": 0.10, "output": 0.40},
    "gemini-3.1-flash-lite": {"label": "Gemini 3.1 Flash Lite", "provider": "google", "input": 0.25, "output": 1.50},
    "gemini-3.5-flash": {"label": "Gemini 3.5 Flash", "provider": "google", "input": 1.50, "output": 9.00},
    "claude-haiku-4-5": {"label": "Haiku 4.5", "provider": "anthropic", "input": 1.0, "output": 5.0, "effort": False},
    "claude-sonnet-4-6": {"label": "Sonnet 4.6", "provider": "anthropic", "input": 3.0, "output": 15.0, "effort": True},
    "claude-opus-4-8": {"label": "Opus 4.8", "provider": "anthropic", "input": 5.0, "output": 25.0, "effort": True},
    "claude-fable-5": {"label": "Fable 5", "provider": "anthropic", "input": 10.0, "output": 50.0, "effort": True},
}
MODEL_ORDER = [
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "claude-fable-5",
]
PROVIDER_LABELS = {"anthropic": "Anthropic", "google": "Google Gemini"}
DEFAULT_MODEL = "gemini-3.1-flash-lite"

MAX_TOKENS = 4096
LOGIN_COOLDOWN = 3.0

# Effort levels. Anthropic effort-capable models accept all of them; Gemini 3.x
# maps effort to thinking_level (low/medium/high). Default is the fastest.
EFFORT_LEVELS = ["low", "medium", "high", "max"]
GEMINI_EFFORT_LEVELS = ["low", "medium", "high"]
DEFAULT_EFFORT = "low"

# Per-prompt price estimate assumptions, converted to PLN with a fixed rate.
ESTIMATE_INPUT_TOKENS = 1000
ESTIMATE_OUTPUT_TOKENS = 1500
USD_TO_PLN = 4.0

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
    reasoning TEXT
)
"""

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
            cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS reasoning TEXT")
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


def build_reasoning_label(model, effort, thinking):
    cfg = MODELS[model]
    if cfg["provider"] == "anthropic":
        if not cfg.get("effort"):
            return None
        parts = [effort.capitalize()] if effort in EFFORT_LEVELS else []
        if model == "claude-fable-5" or thinking == "on":
            parts.append("thinking")
        return " · ".join(parts) if parts else None
    if model.startswith("gemini-3"):
        return effort.capitalize() if effort in GEMINI_EFFORT_LEVELS else None
    return None


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def require_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)
    return wrapper


def call_anthropic(model, prompt, effort, thinking):
    cfg = MODELS[model]
    kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if cfg.get("effort"):
        if effort in EFFORT_LEVELS:
            kwargs["output_config"] = {"effort": effort}
        if model != "claude-fable-5" and thinking == "on":
            kwargs["thinking"] = {"type": "adaptive"}

    if model == "claude-fable-5":
        try:
            resp = anthropic_client.beta.messages.create(
                betas=["server-side-fallback-2026-06-01"],
                fallbacks=[{"model": "claude-opus-4-8"}],
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


def build_gemini_thinking(reasoning):
    level = {"low": "low", "medium": "medium", "high": "high", "max": "high"}.get(reasoning)
    if not level:
        return None
    try:
        return genai_types.ThinkingConfig(thinking_level=level)
    except Exception:
        return None


def call_gemini(model, prompt, effort):
    if gemini_client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    config_args = dict(system_instruction=SYSTEM_PROMPT, max_output_tokens=MAX_TOKENS)
    if model.startswith("gemini-3"):
        thinking = build_gemini_thinking(effort)
        if thinking is not None:
            config_args["thinking_config"] = thinking

    resp = gemini_client.models.generate_content(
        model=model,
        contents=prompt,
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


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.post("/api/login")
def login():
    ip = client_ip()
    now = time.monotonic()
    with _login_lock:
        wait = LOGIN_COOLDOWN - (now - _last_attempt.get(ip, 0))
        if wait > 0:
            return jsonify(error="too_many_attempts", retry_after=round(wait, 1)), 429
        _last_attempt[ip] = now

    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", ""))
    if hmac.compare_digest(pin, APP_PIN):
        session.permanent = True
        session["auth"] = True
        return jsonify(ok=True)
    return jsonify(error="invalid_pin"), 401


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
    effort_options = [{"value": v, "label": v.capitalize()} for v in EFFORT_LEVELS]
    gemini_effort_options = [{"value": v, "label": v.capitalize()} for v in GEMINI_EFFORT_LEVELS]

    models = []
    for model_id in MODEL_ORDER:
        cfg = MODELS[model_id]
        if cfg["provider"] == "anthropic":
            if cfg.get("effort"):
                effort_ctl = {"available": True, "options": effort_options, "default": "low"}
                if model_id == "claude-fable-5":
                    thinking_ctl = {"available": False, "value": "on"}
                else:
                    thinking_ctl = {"available": True, "default": "off"}
            else:
                effort_ctl = {"available": False, "value": "—"}
                thinking_ctl = {"available": False, "value": "off"}
        else:
            if model_id.startswith("gemini-3"):
                effort_ctl = {"available": True, "options": gemini_effort_options, "default": "low"}
            else:
                effort_ctl = {"available": False, "value": "Off"}
            thinking_ctl = {"available": False, "value": "off"}
        models.append({
            "id": model_id,
            "label": cfg["label"],
            "provider": cfg["provider"],
            "provider_label": PROVIDER_LABELS[cfg["provider"]],
            "input": cfg["input"],
            "output": cfg["output"],
            "effort": effort_ctl,
            "thinking": thinking_ctl,
            "est_pln": estimate_pln(cfg),
        })
    return jsonify(
        models=models,
        default=DEFAULT_MODEL,
        usd_pln=USD_TO_PLN,
        max_tokens=MAX_TOKENS,
    )


@app.post("/api/generate")
@require_auth
def generate():
    data = request.get_json(silent=True) or {}
    model = data.get("model", DEFAULT_MODEL)
    prompt = (data.get("prompt") or "").strip()
    if model not in MODELS:
        return jsonify(error="unknown_model"), 400
    if not prompt:
        return jsonify(error="empty_prompt"), 400

    effort = data.get("effort", DEFAULT_EFFORT)
    if effort not in EFFORT_LEVELS:
        effort = DEFAULT_EFFORT
    thinking = data.get("thinking", "off")
    if thinking not in ("on", "off"):
        thinking = "off"

    provider = MODELS[model]["provider"]
    start = time.perf_counter()
    try:
        if provider == "google":
            result = call_gemini(model, prompt, effort)
        else:
            result = call_anthropic(model, prompt, effort, thinking)
    except Exception as exc:  # surface API/network errors to the client
        traceback.print_exc()
        return jsonify(error="api_error", detail=str(exc)), 502
    duration_ms = int((time.perf_counter() - start) * 1000)

    served = result["served"]
    price = MODELS.get(served, MODELS[model])
    in_tok = result["input_tokens"]
    out_tok = result["output_tokens"]
    cost = in_tok / 1_000_000 * price["input"] + out_tok / 1_000_000 * price["output"]

    stored_reasoning = build_reasoning_label(model, effort, thinking)

    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO prompts
               (model, prompt, answer, input_tokens, output_tokens, cost_usd, duration_ms, stop_reason, reasoning)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, created_at""",
            (served, prompt, result["answer"], in_tok, out_tok, cost, duration_ms,
             result["stop_reason"], stored_reasoning),
        )
        row = cur.fetchone()
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
    conn.close()
    if not row:
        return jsonify(error="not_found"), 404
    row["created_at"] = row["created_at"].isoformat()
    row["model_label"] = MODELS.get(row["model"], {}).get("label", row["model"])
    return jsonify(row)


@app.delete("/api/history/<int:item_id>")
@require_auth
def delete_history_item(item_id):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM prompts WHERE id = %s", (item_id,))
    conn.close()
    return jsonify(ok=True)


init_db()
