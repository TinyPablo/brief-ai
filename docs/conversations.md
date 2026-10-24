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

Titles are generated asynchronously after the first successful answer. Until a
title is available, navigation uses a first-question preview.

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

Navigation shows an arrow and the adjacent conversation's title, without a
central heading or tab strip. At narrow widths, adjacent links move above the
conversation. Horizontal touch swipes and trackpad gestures navigate one view
per gesture. Vertical scrolling, pinch zoom, text selection, editors, links,
buttons, code blocks and open dialogs keep their own interactions. View changes
use directional slide transitions; reduced-motion preferences disable them.
The interface is displayed at 120% of its earlier scale.

The main composer does not contain context controls. Date/time, location and
personal context are configured in Settings and are captured when a conversation
starts. The session diagnostics panel is not displayed.

## Automatic titles

The answer is committed before title generation. A separate durable title state
on `conversations` (`waiting`, `queued`, `running`, `completed`, `failed`) lets a
worker generate a title without delaying answer delivery. Browser polling
continues while the title is pending. Normal answer jobs take priority over title
jobs.

The title request contains at most 2,000 characters of the first question and
2,000 characters of its answer. It excludes Settings context and attachments.
It requests a short title in the question's language. Cloud titles use a small
model from the first answer's provider: Haiku 4.5, Gemini 3.1 Flash Lite, or GPT-5.4
Nano. Local conversations use their local model. The title is stored once and
is not regenerated on follow-ups.

Title generation is an additional model request. Its served model, input/output
tokens and cost are stored separately in `title_model`, `title_input_tokens`,
`title_output_tokens` and `title_cost_usd`. Per-answer costs exclude that metadata
request. If it fails or returns an unsuitable title, navigation keeps the question
preview. Interrupted title requests become failed, without automatic paid retries.
Deleting a conversation during title generation discards the title result.

The migration adds title state and usage columns to `conversations`. Existing
conversations are not batch-processed for titles; a later successful follow-up
can queue a missing title. Legacy standalone history remains unchanged.

## Branches

The Branch action on any assistant message creates an independent conversation
containing all messages and attachments up to and including that answer. The
source conversation remains unchanged. The copy opens immediately to its right.
The `POST /api/conversations/<id>/branches` body contains `message_id` and a
client-generated UUID `request_id`; retries return the same branch. Reusing an
ID for a different source returns HTTP 409.

Messages receive new IDs while keeping their content, timestamps, model and
original usage metadata. Copied rows have `inherited = true` and do not represent
new provider requests. Future branch replies have `inherited = false`.
Attachment associations are copied, while image bytes remain deduplicated.
Settings context is copied into the independent conversation. Source IDs are
retained as provenance without foreign keys, so deleting the source cannot
remove the branch. A branch with an existing title receives a short branch suffix.

## Public snapshots

Share offers three scopes: `answer` (only the selected answer), `exchange`
(the immediate question and answer), and `conversation` (all messages through
the selected answer). The default is `conversation`. Attachments belonging to
included messages are always included; no separate image checkbox is shown.
Settings context, diagnostics and usage/cost fields are excluded.

Publishing stores an immutable JSON snapshot and references to the included
source messages and images. Later follow-ups do not change it. Update snapshot
explicitly replaces its contents under the same URL. Stop sharing revokes it.
The Shared tab lists active links, and public pages render the stored messages
as a conversation.

Deleting any included source message revokes the entire snapshot. Deleting a
later message outside the snapshot leaves its link active. Deleting a source
conversation revokes its links but leaves branch conversations and their links
independent. Postgres deletion triggers invalidate snapshots even when source
messages are removed outside the API. Image authorization checks the snapshot's
own attachment references, so a token cannot retrieve other conversation images.

`GET /api/conversation-requests/<request_id>` recovers the conversation created
by a submission. Sharing settings use `/api/conversation-messages/<message_id>`
and its `/share` subroute. POST/PATCH accept `{ "scope": "conversation" }`,
`{ "scope": "exchange" }`, or `{ "scope": "answer" }`.
`/api/conversation-shares` lists active links. Older single-answer links and
legacy standalone sharing remain readable; updating a conversation link creates
a snapshot. Existing older-client question visibility flags remain accepted,
while attachments always follow included messages in new snapshots.

## Composer controls

New and Close are always visible in the sticky application header. Close removes
only the open view and asks before discarding a draft. A completely empty view
is removed automatically when another view or another application section is
selected; a view with text, attachments or a pending submission is kept.
Clicking New while already in an empty view gives a short scale acknowledgement.

Transitions clip both panes at the chat boundaries, with synchronized slide and
fade animations. The outgoing clipping window remains stationary, preventing the
old pane from covering the incoming pane or crossing the side navigation.
Reduced-motion preferences disable these animations. The session diagnostics
panel has been removed from the interface, including Settings.

An optional browser check requires Playwright and Chrome and uses fake API
responses without model calls:

```bash
python3 backend/tests/check_conversation_ui.py
python3 backend/tests/check_session_storage.py
python3 backend/tests/check_conversation_sessions.py
python3 backend/tests/check_conversation_navigation.py
python3 backend/tests/check_conversation_fork_ui.py
PYTHONPATH=backend python3 backend/tests/check_conversation_forks.py
```

The existing `/api/generate` endpoint remains available for older clients.
Rolling back requires stopping the worker and restoring the previous backend
and frontend images; the additive tables can remain in place.
