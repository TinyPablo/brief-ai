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

Titles remain unset at this stage. Navigation uses the first question as a
preview; automatic title generation is a separate change.

## Local verification

The worker uses the backend image and environment. After building this stage:

```bash
docker compose up -d --build backend worker frontend
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

## Browser workspace

The browser keeps an ordered set of open conversation views for each tab.
Opening History selects an existing view or inserts one immediately to the right
of the active view. New conversation reuses an empty view without a draft or
attachments; otherwise it inserts a new one on the right. Closing a view selects
its right neighbor when available, otherwise its left neighbor. Closing the last
view leaves an empty composer.

Mounted views retain their messages in memory. Switching a settled conversation
does not fetch it again. Pending jobs continue polling in their own views while
other conversations can submit requests. Closing a view stops its polling but
does not cancel server-side generation or delete sent messages.

`sessionStorage` stores a workspace identifier, view order, conversation IDs and
selected models. Native IndexedDB stores drafts, binary attachments and cached
conversation snapshots, keyed by workspace and view. Message text and image bytes
are not placed in `sessionStorage`. Refresh restores the views and drafts, then
revalidates saved conversations against the server. A pending submission ID lets
the view recover a request whose HTTP acknowledgement was interrupted.

Each view retains its own selected model and scroll position. A new reply follows
the bottom only when that view was already at the bottom. Reading earlier
messages keeps its position and exposes a New reply action. Background views get
an unread indicator on their navigation controls.

Closing a view with unsent text, attachments or a pending submission requires
confirmation. Its local draft, attachments and snapshot are then removed; server
history remains available. A new browser-tab session does not restore a previous
session's drafts. A BroadcastChannel ownership check separates copied session
identifiers when duplicating a live tab. Session records not touched for seven
days are cleaned up on workspace initialization. Open workspaces refresh their
lifetime once a minute. Physical deletion on browser close is not assumed.

When browser storage is unavailable or full, views continue to work in memory
and display a persistence warning. The user can continue sending messages;
unsent changes may not survive refresh in that state.

Navigation currently uses adjacent first-question previews and simple arrows.
Generated titles, touch gestures and animated transitions are separate changes.

Individual assistant answers retain public sharing. A link exposes only that
answer and, optionally, its immediate user question and images. Earlier turns,
Settings context and diagnostics are never included. Deleting a shared answer
through truncation revokes its link. The Shared tab lists both conversation
answers and earlier standalone links.

`GET /api/conversation-requests/<request_id>` recovers the conversation created
by a submission. Sharing settings use `/api/conversation-messages/<message_id>`
and its `/share` subroute, and `/api/conversation-shares` lists active links.

An optional browser check requires Playwright and Chrome and uses fake API
responses without model calls:

```bash
python3 backend/tests/check_conversation_ui.py
python3 backend/tests/check_session_storage.py
python3 backend/tests/check_conversation_sessions.py
```

The existing `/api/generate` endpoint remains available for older clients.
Rolling back requires stopping the worker and restoring the previous backend
and frontend images; the additive tables can remain in place.
