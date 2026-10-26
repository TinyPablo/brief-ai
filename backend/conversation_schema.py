"""Additive storage for conversations; legacy prompts remain independent."""

CONVERSATION_MIGRATIONS = (
    """CREATE TABLE IF NOT EXISTS conversations (
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        title TEXT,
        context TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS conversation_messages (
        id UUID PRIMARY KEY,
        conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position >= 0),
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        model TEXT,
        brief BOOLEAN NOT NULL DEFAULT FALSE,
        input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
        output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
        cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
        duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
        stop_reason TEXT,
        reasoning TEXT,
        UNIQUE (conversation_id, position),
        UNIQUE (conversation_id, id)
    )""",
    """CREATE TABLE IF NOT EXISTS conversation_message_images (
        message_id UUID NOT NULL REFERENCES conversation_messages(id) ON DELETE CASCADE,
        image_sha TEXT NOT NULL REFERENCES images(sha256) ON DELETE RESTRICT,
        position INTEGER NOT NULL CHECK (position >= 0),
        PRIMARY KEY (message_id, position)
    )""",
    """CREATE INDEX IF NOT EXISTS conversation_message_images_sha_idx
       ON conversation_message_images(image_sha)""",
    """CREATE TABLE IF NOT EXISTS conversation_jobs (
        id UUID PRIMARY KEY,
        conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        user_message_id UUID NOT NULL UNIQUE,
        model TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        heartbeat_at TIMESTAMPTZ,
        error_code TEXT,
        error_detail TEXT,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        FOREIGN KEY (conversation_id, user_message_id)
            REFERENCES conversation_messages(conversation_id, id) ON DELETE CASCADE
    )""",
    # Serialize follow-ups within one conversation, while allowing other chats to run.
    """CREATE UNIQUE INDEX IF NOT EXISTS conversation_jobs_active_idx
       ON conversation_jobs(conversation_id) WHERE status IN ('queued', 'running')""",
    """CREATE INDEX IF NOT EXISTS conversation_jobs_queue_idx
       ON conversation_jobs(created_at, id) WHERE status = 'queued'""",
    """CREATE INDEX IF NOT EXISTS conversations_updated_idx
       ON conversations(updated_at DESC, id)""",
)

CONVERSATION_MIGRATIONS += (
    'ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS share_token TEXT',
    'ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS share_show_prompt BOOLEAN NOT NULL DEFAULT TRUE',
    'ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS share_show_images BOOLEAN NOT NULL DEFAULT FALSE',
    '''CREATE UNIQUE INDEX IF NOT EXISTS conversation_messages_share_idx
       ON conversation_messages(share_token) WHERE share_token IS NOT NULL''',
)


CONVERSATION_MIGRATIONS += (
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_status TEXT NOT NULL DEFAULT 'waiting' CHECK (title_status IN ('waiting', 'queued', 'running', 'completed', 'failed'))",
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_model TEXT',
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_input_tokens INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_output_tokens INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0',
    "CREATE INDEX IF NOT EXISTS conversations_title_queue_idx ON conversations(created_at) WHERE title_status = 'queued'",
    "CREATE INDEX IF NOT EXISTS conversations_title_running_idx ON conversations(id) WHERE title_status = 'running'",
)

CONVERSATION_MIGRATIONS += (
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS branch_request_id UUID',
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS branch_source_conversation_id UUID',
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS branch_source_message_id UUID',
    'CREATE UNIQUE INDEX IF NOT EXISTS conversations_branch_request_idx ON conversations(branch_request_id) WHERE branch_request_id IS NOT NULL',
    'ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS inherited BOOLEAN NOT NULL DEFAULT FALSE',
    '''CREATE TABLE IF NOT EXISTS conversation_share_snapshots (
        message_id UUID PRIMARY KEY REFERENCES conversation_messages(id) ON DELETE CASCADE,
        token TEXT NOT NULL UNIQUE,
        scope TEXT NOT NULL CHECK (scope IN ('answer', 'exchange', 'conversation')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload JSONB NOT NULL,
        revoked BOOLEAN NOT NULL DEFAULT FALSE
    )''',
    'ALTER TABLE conversation_share_snapshots ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT FALSE',
    '''CREATE TABLE IF NOT EXISTS conversation_share_sources (
        snapshot_id UUID NOT NULL REFERENCES conversation_share_snapshots(message_id) ON DELETE CASCADE,
        message_id UUID NOT NULL REFERENCES conversation_messages(id) ON DELETE CASCADE,
        PRIMARY KEY (snapshot_id, message_id)
    )''',
    'CREATE INDEX IF NOT EXISTS conversation_share_sources_message_idx ON conversation_share_sources(message_id)',
    '''CREATE TABLE IF NOT EXISTS conversation_share_images (
        snapshot_id UUID NOT NULL REFERENCES conversation_share_snapshots(message_id) ON DELETE CASCADE,
        image_sha TEXT NOT NULL REFERENCES images(sha256) ON DELETE RESTRICT,
        PRIMARY KEY (snapshot_id, image_sha)
    )''',
    'CREATE INDEX IF NOT EXISTS conversation_share_images_sha_idx ON conversation_share_images(image_sha)',
    '''CREATE OR REPLACE FUNCTION revoke_conversation_snapshots() RETURNS trigger AS $$
       BEGIN
           UPDATE conversation_share_snapshots SET revoked = TRUE
           WHERE message_id IN (SELECT snapshot_id FROM conversation_share_sources WHERE message_id = OLD.id);
           DELETE FROM conversation_share_images
           WHERE snapshot_id IN (SELECT snapshot_id FROM conversation_share_sources WHERE message_id = OLD.id);
           DELETE FROM conversation_share_sources
           WHERE snapshot_id IN (SELECT snapshot_id FROM conversation_share_sources WHERE message_id = OLD.id);
           RETURN OLD;
       END;
       $$ LANGUAGE plpgsql''',
    'DROP TRIGGER IF EXISTS revoke_conversation_snapshots ON conversation_messages',
    '''CREATE TRIGGER revoke_conversation_snapshots BEFORE DELETE ON conversation_messages
       FOR EACH ROW EXECUTE FUNCTION revoke_conversation_snapshots()''',
)

CONVERSATION_MIGRATIONS += (
    'ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ',
)

CONVERSATION_MIGRATIONS += (
    """CREATE TABLE IF NOT EXISTS conversation_mutations (
        request_id UUID PRIMARY KEY,
        conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        message_id UUID NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('edit', 'regenerate')),
        response JSONB NOT NULL,
        status INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
)
