#!/usr/bin/env python3
"""Exercise the same bounded API used by other apps; never executes model output."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

DEFAULT_PROMPT = '''Find the bug across these two Python files. Explain it and return a minimal fix.
The first call should return [1] and the second [2], but the second returns [1, 2].

# store.py
def collect(value, items=[]):
    items.append(value)
    return items

# main.py
from store import collect
print(collect(1))
print(collect(2))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8790/api/local/v1")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--max-tokens", type=int, default=256)
    args = parser.parse_args()
    key = os.environ.get("LOCAL_AI_API_KEY")
    if not key:
        parser.error("Set LOCAL_AI_API_KEY in the environment (do not pass it on the command line).")
    prompt = args.prompt_file.read_text() if args.prompt_file else DEFAULT_PROMPT
    body = {"model": "local-qwen2.5-coder-1.5b-q4_k_m", "messages": [{"role": "user", "content": prompt}], "max_tokens": args.max_tokens}
    request = urllib.request.Request(args.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=260) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode()}")
    elapsed = time.monotonic() - start
    print(data["choices"][0]["message"]["content"])
    print(json.dumps({"elapsed_seconds": round(elapsed, 2), "usage": data.get("usage"),
        "finish_reason": data["choices"][0]["finish_reason"],
        "note": "Wall time includes prompt processing; not a decode-only speed measurement."}, indent=2))


if __name__ == "__main__":
    main()
