"""Bounded, single-request access to a private llama.cpp server (Linux/macOS)."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

MODEL_ID = "local-qwen2.5-coder-1.5b-q4_k_m"
CONTEXT = 8192
MAX_OUTPUT = 1024
DEADLINE = 240
MAX_INPUT_BYTES = 128_000


class LocalError(Exception):
    def __init__(self, code, detail, status=503):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def generate(messages, max_tokens=MAX_OUTPUT):
    if not os.environ.get("LOCAL_AI_URL"):
        raise LocalError("local_unavailable", "Local AI is not enabled.")
    if (not isinstance(messages, list) or not messages or len(messages) > 100
            or any(not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"}
                   or not isinstance(m.get("content"), str) for m in messages)):
        raise LocalError("invalid_messages", "Use text messages with system, user, or assistant roles.", 400)
    messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    if sum(len(m["content"].encode()) for m in messages) > MAX_INPUT_BYTES:
        raise LocalError("local_context_exceeded", "The input is too large. Send fewer or smaller files.", 400)
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_OUTPUT:
        raise LocalError("invalid_max_tokens", f"max_tokens must be between 1 and {MAX_OUTPUT}.", 400)
    # A file lock covers every gunicorn worker, unlike threading.Lock.
    with open(os.environ.get("LOCAL_AI_LOCK", "/tmp/brief-local-ai.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LocalError("local_busy", "Local AI is busy. Try again after the current answer finishes.", 429)
        try:
            # Killing the child closes its inference connection. The wall-clock
            # deadline includes tokenization, prefill and generation, not just
            # socket idle time. No automatic retry or unbounded request queue.
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                input=json.dumps({"messages": messages, "max_tokens": max_tokens}),
                text=True, capture_output=True, timeout=DEADLINE, check=True,
            )
            data = json.loads(result.stdout)
            if "local_error" in data:
                raise LocalError(**data["local_error"])
            return data
        except subprocess.TimeoutExpired:
            raise LocalError("local_timeout", "Local AI reached its four-minute time limit. Try a shorter input.", 504)
        except (subprocess.CalledProcessError, ValueError):
            raise LocalError("local_unavailable", "Local AI is unavailable. Check the inference service.")


def infer(client, payload):
    def post(path, data):
        response = client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    available = client.get("/slots?fail_on_no_slot=1")
    if available.status_code == 503:
        raise LocalError("local_busy", "Local AI is loading or finishing another request. Try again shortly.", 429)
    available.raise_for_status()
    rendered = post("/apply-template", {"messages": payload["messages"]})["prompt"]
    tokens = post("/tokenize", {"content": rendered, "add_special": True, "parse_special": True})["tokens"]
    # Leave a small safety margin for template/BOS differences between endpoints.
    if len(tokens) + payload["max_tokens"] + 32 > CONTEXT:
        raise LocalError("local_context_exceeded", "Input plus reserved output exceeds the 8,192-token context. Send fewer or smaller files.", 400)
    # Stream internally so closing the worker connection interrupts generation.
    # The website and external API still receive one complete response.
    answer = []
    result = {"id": "", "object": "chat.completion", "created": 0, "model": MODEL_ID}
    usage = None
    finish = None
    done = False
    with client.stream("POST", "/v1/chat/completions", json={
        **payload, "model": MODEL_ID, "stream": True,
        "stream_options": {"include_usage": True}, "temperature": 0.2,
        "cache_prompt": False, "sse_ping_interval": 1,
    }) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            if line == "data: [DONE]":
                done = True
                break
            chunk = json.loads(line[6:])
            if "error" in chunk:
                raise LocalError("local_unavailable", "Local AI could not complete this request.")
            result["id"] = chunk.get("id", result["id"])
            result["created"] = chunk.get("created", result["created"])
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices", []):
                text = choice.get("delta", {}).get("content")
                if text:
                    answer.append(text)
                finish = choice.get("finish_reason") or finish
    if not done or not usage or not finish:
        raise LocalError("local_unavailable", "Local AI returned an incomplete response. Please retry.")
    result["usage"] = usage
    result["choices"] = [{"index": 0, "message": {"role": "assistant", "content": "".join(answer)}, "finish_reason": finish}]
    return result


if __name__ == "__main__":
    import httpx
    try:
        with httpx.Client(base_url=os.environ["LOCAL_AI_URL"].rstrip("/"),
                          timeout=httpx.Timeout(DEADLINE, connect=5), trust_env=False) as client:
            print(json.dumps(infer(client, json.load(sys.stdin))))
    except LocalError as exc:
        print(json.dumps({"local_error": {"code": exc.code, "detail": exc.detail, "status": exc.status}}))
    except Exception:
        # Never return internal URLs, credentials, or raw upstream errors.
        sys.exit(1)
