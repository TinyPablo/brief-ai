# Local AI

This optional feature runs Qwen2.5-Coder-1.5B-Instruct Q4_K_M through a pinned
llama.cpp build. The ordinary Compose stack stays cloud-only unless the overlay
is supplied. No training or cloud inference is involved in the local route.

## Resource budget

- One CPU-equivalent quota, one inference thread and one prompt-processing thread.
- 3 GiB memory ceiling; the equal memory-plus-swap ceiling disables container swap.
- Prompt batches are limited to 32 tokens so cancellation can be processed between
  short CPU batches; this favors responsiveness over maximum prefill throughput.
- One generation slot. A nonblocking file lock shared by backend workers rejects
  concurrent website/API requests with HTTP 429 instead of queuing them.
- 8,192-token context, including template, prompt and output; up to 1,024 output
  tokens. The server's own tokenizer checks the rendered template before inference,
  with a 32-token safety margin. Oversized requests fail without truncation.
- A 240-second subprocess deadline covers tokenization, prefill and generation.
  Timeout kills the HTTP client process and closes its streaming model connection.
  SSE heartbeats are configured at one second, but upstream cancellation is not
  instantaneous: the HTTP deadline bounds the caller wait, while model cleanup
  can take additional seconds. New requests are rejected until the slot is free.
  Streaming is internal; callers receive a complete JSON response. There are
  no automatic retries. Slot readiness is checked before accepting new work.
- Closing a browser tab does **not** immediately cancel a synchronous backend
  request: it can continue until completion or the deadline. Explicit cancellation
  and streaming are not implemented in this first version.

The limit bounds scheduled CPU time, not temperature or exact impact on other
services. The Lenovo has two physical cores with SMT; benchmark under realistic
load. Build-time compilation is separate from runtime limits (single compiler job).
Do not assume Docker build downloads and compilation obey the runtime CPU quota.

## Prepare and run

Requires Docker Compose and Python 3.11+ for the download helper. Use an isolated
test checkout/project until the feature has been validated. Do not deploy to the
live stack just to try the commands below.

```bash
python3 scripts/download-local-model.py
docker compose -f docker-compose.yml -f docker-compose.local-ai.yml build
docker compose -f docker-compose.yml -f docker-compose.local-ai.yml up -d
```

The helper downloads about 1.12 GB once and verifies the published SHA-256 before
installing the model. Weights live in ignored `models/`, mounted read-only. The
runtime has no host port and belongs only to an internal Docker network. It also
runs with `--offline`; startup uses the local GGUF path, never a model hub ID.

The website lists **Local AI / Qwen Coder 1.5B (local)** when enabled. Paste text
and code with filenames into the prompt. File uploading/indexing, image input,
agents and tool execution are not included. Local answers are saved in History;
their provider charge is zero (electricity and hardware are not estimated).

Optional `.env` settings:

```dotenv
DEFAULT_MODEL=local-qwen2.5-coder-1.5b-q4_k_m
LOCAL_AI_API_KEY=replace-with-a-long-random-secret
```

Generate a key locally using `python3 -c 'import secrets; print(secrets.token_hex(32))'`.
Leave `LOCAL_AI_API_KEY` empty to disable access from other applications. Cloud
keys are not required to boot the backend, but cloud selections still require
their respective keys. A local failure never falls back to a cloud model.

## API for other applications

Base URL: `http://127.0.0.1:8790/api/local/v1` on the server.

- `GET /models`
- `POST /chat/completions`
- Authentication: `Authorization: Bearer <LOCAL_AI_API_KEY>`.
- Supported request fields: `model`, `messages`, `max_tokens`, `stream=false`.
- Messages contain text and roles `system`, `user`, or `assistant`.
- Unsupported options (including streaming and tools) are rejected explicitly.
- Direct API calls are not saved to website History.

This is a small compatible subset, not a complete replacement for every provider
API. It shares admission control, context checks and deadlines with the website.
The existing site reverse proxy also exposes this authenticated route if enabled;
do not put the raw inference server on a public port.

For initial remote testing, bypass external proxies with an SSH tunnel:

```bash
ssh -N -L 8791:127.0.0.1:8790 pawel@lenovo-server
```

Use `http://127.0.0.1:8791/api/local/v1` for the API. Browser login over plain
HTTP requires `COOKIE_SECURE=false` in an isolated test stack; keep production
cookies secure and use HTTPS for the production website.
An external proxy can impose a timeout shorter than our four-minute deadline;
test long generations through the tunnel before diagnosing inference failures.

## Benchmark and acceptance

Set `LOCAL_AI_API_KEY` in your shell without sharing it in logs, then run:

```bash
python3 scripts/benchmark-local-ai.py
python3 scripts/benchmark-local-ai.py --prompt-file your-code-question.txt --max-tokens 512
docker stats --no-stream  # inspect the local-ai container
```

Run the small benchmark twice to distinguish cold startup/cache costs from a
second request. Then try a real multi-file bug. Review the answer manually and
test proposed fixes yourself; the script never executes generated code.

Acceptance on the Lenovo requires useful fixes within four minutes, stable memory
without OOM/restart loops, responsive other services, and clean 429/400/504 errors
for busy/context/timeout cases. A passing unit suite does not establish model
quality or speed on that machine. If it fails, reduce input/output first; do not
automatically raise the CPU/RAM limits or silently switch to cloud inference.

## Offline operation and backups

Frontend libraries, styles and fonts are served from checked-in `frontend/public/vendor`.
`manifest.json` records source URLs and SHA-256 hashes; dependency licenses are
alongside them. `scripts/vendor-frontend.py` is an explicit online refresh tool,
not part of startup or Docker builds.

Keep separate backups of the verified GGUF, its Apache 2.0 license, application
source, `.env`, database and built Docker images **for the target architecture**.
An image built on an ARM Mac cannot be assumed to run natively on the Intel Lenovo.
Offline restart works from existing artifacts; rebuilding from Dockerfiles still
requires package registries unless their dependencies are also archived.

Test without internet using direct LAN/SSH access and the local model. A public
domain, tunnel provider, remote image embedded in an answer, or cloud model remains
an external dependency. Self-contained inference does not make a remotely hosted
machine independent of its host, electricity or networking.

## Sources

- [Model and license](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF)
- [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/8345f333951c661d166b00e6f9362e553768f292/tools/server)
- [Docker resource limits](https://docs.docker.com/engine/containers/resource_constraints/)
