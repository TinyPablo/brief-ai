# Conversations

New conversations use `conversations`, `conversation_messages`,
`conversation_message_images`, and `conversation_jobs`. Existing `prompts` and
their public links remain independent and cannot be continued through this API.
Images share the existing content-addressed storage.

## Generation lifecycle

The authenticated API persists the user message and a queued job in one
transaction, then returns HTTP 202. The `worker` Compose service performs model
requests independently of the browser and web workers. Up to four conversations
can generate concurrently by default; `CONVERSATION_WORKERS` accepts 1-8.
Only one queued or running job per conversation is allowed.

Refresh or closing the browser does not cancel a job. Polling the conversation
returns its messages and latest job, including completion and failure state.
Each message has its own server timestamp. Assistant messages retain the model
actually used, token counts, cost and generation duration.

Workers use Postgres session locks to distinguish live jobs from interrupted
ones. A worker or database restart can interrupt a provider request; such a job
becomes `failed` when another worker observes that its lock has disappeared.
Retries require an explicit API call because the original provider request may
already have incurred a charge. Queued jobs survive worker restarts.

Deleting a user message also deletes subsequent messages and their jobs. A
conversation revision and row lock prevent an in-flight result from restoring
deleted content. Deleting the first user message deletes the conversation.
The provider request may still finish and incur a charge after deletion; its
result is discarded. Images are deleted only when neither legacy history nor a
conversation references them.

## API

All routes require the existing authenticated session.

| Method | Route | Behavior |
| --- | --- | --- |
| POST | `/api/conversations` | Create a conversation and enqueue its first turn |
| GET | `/api/conversations?q=&before=` | List conversations, 30 per page; `before` is the last conversation ID |
| GET | `/api/conversations/<id>` | Read messages, attachment metadata and the latest job |
| POST | `/api/conversations/<id>/messages` | Append a follow-up after the last assistant response |
| POST | `/api/conversations/<id>/retry` | Explicitly retry the latest failed job |
| DELETE | `/api/conversations/<id>/messages/<message_id>` | Delete a user message and the rest of the conversation |

Create and append bodies contain `request_id` (a client-generated UUID),
`prompt`, `model`, optional `brief`, and optional `images` using the existing
upload format. Reusing a request ID returns the original job without duplicating
the message. A request ID belongs to a single submission and must not be reused
for different content. An append to a different conversation with the same ID
returns HTTP 409.

`context` is captured when the conversation is created and remains stable for
its follow-ups. Complete user/assistant history and previous images are supplied
to the selected provider in its native message format. Changing providers is
supported; choosing a text-only model for a conversation containing images is
rejected. History is never silently truncated. The API limits user messages to
100,000 characters, follow-up histories to 500,000 stored characters and 100
messages; provider context limits may be lower.

Titles remain unset at this stage. UI navigation and title generation are
separate changes.

## Local verification

The worker uses the backend image and environment. After building this stage:

```bash
docker compose up -d --build backend worker
docker compose ps
```

For installations using Local AI, use both Compose files. The overlay connects
the worker to the inference network and shares an inference lock between the
backend and worker. The worker publishes no host port and adds no persistent
application data volume; all conversation data is in `db_data`.

```bash
python3 -m pytest backend/tests -q
PYTHONPATH=backend python3 backend/tests/check_conversation_schema.py
PYTHONPATH=backend python3 backend/tests/check_conversation_api.py
```

The last two commands require `DATABASE_URL`. The schema check rolls back its
transaction. The API check uses and removes an isolated temporary schema, stubs
providers, and does not issue real model requests. Run these checks against a
local test database.

The existing `/api/generate` and frontend remain available until the conversation
UI is connected. Rolling back this stage requires stopping the worker and
restoring the previous backend image; the additive tables can remain in place.
