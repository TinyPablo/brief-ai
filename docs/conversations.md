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

Cloud generation has a shared 120-second budget, including chart tool rounds and
application retries. SDK retries are disabled. Explicit temporary provider errors
can be retried once within the remaining budget. Network timeouts and permanent
errors are not retried automatically. Each new request receives the remaining
time as its SDK timeout; title generation uses the same bounded provider helpers.
Local AI retains its separate four-minute limit.

An independent worker monitor checks cloud jobs every second and marks overdue
running jobs as failed. This remains active when all generation threads are
blocked. A timeout does not guarantee that the provider has cancelled remote
processing or billing. Late results cannot replace a failure or complete a newer
retry attempt. A queued retry waits for the previous attempt's session lock,
without preventing unrelated conversations from being processed.

Failures expose safe categories and messages for provider overload, rate limits,
exhausted quota, rejected access, incompatible requests, connection failures and
timeouts. The existing Try again control preserves the user message. Provider
payloads and raw error messages are not returned to the browser.

Deleting a user message also deletes subsequent messages and their jobs. A
conversation revision and row lock prevent an in-flight result from restoring
deleted content. Deleting the first user message deletes the conversation.
The provider request may still finish and incur a charge after deletion; its
result is discarded. Images are deleted only when neither legacy history nor a
conversation references them.

## Message editing

User and assistant messages are edited inside their own cards. Opening an editor
keeps the full conversation visible until Save. Leaving the conversation or
cancelling discards the edit; the independent composer draft and attachments stay
intact. Only one message can be edited at a time. Sending a new turn is disabled
while editing, but writing the next draft remains available.

User-message editors support adding and removing images and selecting the reply
model, initially the model of the original answer. Assistant-message editors
change text only. Saving an unchanged message does not truncate history, revoke
links, or generate an answer. Text, attachment order/content and reply-model
changes count as changes.

Saving a changed message preserves its ID and original timestamp, records
`edited_at`, and atomically removes all later messages and jobs. User edits queue
a new answer in the same transaction and update the selected model for subsequent
turns. Assistant edits leave the next turn to the user. Snapshots containing the
edited or removed messages are revoked; earlier snapshots and independent branches
are unaffected. Original generation metrics describe the model call, not manual
edits. Pending results from an obsolete revision cannot restore removed history.

Edits submit the conversation revision and reject stale revisions with HTTP 409.
A durable mutation receipt makes repeated saves with the same request ID
idempotent, including assistant edits and unchanged saves. New image bytes are
validated before replacement; retained image references must belong to the edited
message. Unreferenced images are cleaned up without affecting branches or other
conversations. Image-only user messages are supported for creation, follow-ups and
edits; a message without text or attachments is rejected.

Deleting a user message is confirmed inline: the trash icon turns red with a small
question badge and a shrinking five-second ring. A single pulse draws attention;
the icon does not rotate or indicate completed deletion. Reduced-motion preferences
disable the pulse and ring animation without changing the confirmation timeout. A second click deletes the message and later turns.
Clicking elsewhere, moving focus away, Escape, leaving the conversation, or five
seconds of inactivity restores the trash icon without deleting anything. Pending
deletions disable repeated clicks and display a spinner in the same button.

## Regeneration and recovery

Assistant-message header actions use icons for Copy, Edit, Regenerate, Branch and
Share. Regenerate opens one anchored panel with a native model select and confirmation.
The same select style is used in the prompt composer and message editor. Scrolling
the conversation dismisses the panel instead of repositioning it.
Changing the selection does not replace the panel or change the conversation.
Outside click, Escape and the cancel icon dismiss it without applying changes.
Confirming removes that assistant response and all later turns, keeps its preceding
question and images, and atomically queues a replacement using the selected model.
The selected model also becomes the default for subsequent follow-ups. Regeneration
uses the same revision checks, durable receipts, share revocation and branch
independence as editing.

A failed generation displays an explicit Try again action beneath the stored user
message. It retries the existing job rather than inserting another question.
Background polling reconnects to pending work after a lost connection. If the
initial submission acknowledgement is lost, Try again resends the captured request
with its original request ID so the server returns the same conversation/job.
An uncertain edit or regeneration keeps its request locked until resolved, allowing
Try again without silently changing the body of an already accepted request.

Attachment buttons display a paperclip and optional image count. Size and limits
appear on hover or keyboard focus and close after clicking or changing attachments. The save indicator includes a checkmark.

## API

All routes require the existing authenticated session.

| Method | Route | Behavior |
| --- | --- | --- |
| POST | `/api/conversations` | Create a conversation and enqueue its first turn |
| GET | `/api/conversations?q=&before=` | List conversations, 30 per page; `before` is the last conversation ID |
| GET | `/api/conversations/<id>` | Read messages, attachment metadata and the latest job |
| POST | `/api/conversations/<id>/messages` | Append a follow-up after the last assistant response |
| POST | `/api/conversations/<id>/retry` | Explicitly retry the latest failed job |
| PATCH | `/api/conversations/<id>/messages/<message_id>` | Edit with `content`, `revision`, `request_id` and optional `model`; truncate later messages and queue a response for user edits |
| POST | `/api/conversations/<id>/messages/<message_id>/regenerate` | Confirm replacement of an assistant answer with `revision`, `request_id` and `model` |
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
PYTHONPATH=backend python3 backend/tests/check_conversation_editing.py
python3 backend/tests/check_conversation_actions.py
```

The schema, API and editing checks require `DATABASE_URL`. The schema check rolls back its
transaction. The API and editing checks use and remove isolated temporary schemas, stub
providers, and do not issue real model requests. The actions check uses mocked HTTP
responses in Chrome and verifies editing, regeneration, recovery and mobile layout. Run these checks against a
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
