"""Run directly against a disposable schema using DATABASE_URL; rolls back all changes."""

import os
import uuid

import psycopg2

from conversation_schema import CONVERSATION_MIGRATIONS


def check_schema():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            schema = "conversation_check_" + uuid.uuid4().hex
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET LOCAL search_path TO "{schema}"')
            cur.execute("CREATE TABLE images (sha256 TEXT PRIMARY KEY)")
            for _ in range(2):
                for statement in CONVERSATION_MIGRATIONS:
                    cur.execute(statement)

            conversation, other, message, job = [str(uuid.uuid4()) for _ in range(4)]
            cur.execute("INSERT INTO conversations(id) VALUES (%s), (%s)", (conversation, other))
            cur.execute(
                "INSERT INTO conversation_messages(id, conversation_id, position, role, content) "
                "VALUES (%s, %s, 0, 'user', 'question')", (message, conversation),
            )
            cur.execute(
                "INSERT INTO conversation_jobs(id, conversation_id, user_message_id, model, revision) "
                "VALUES (%s, %s, %s, 'test-model', 0)", (job, conversation, message),
            )

            def rejects(sql, params, error):
                cur.execute("SAVEPOINT invalid_write")
                try:
                    cur.execute(sql, params)
                except error:
                    cur.execute("ROLLBACK TO SAVEPOINT invalid_write")
                else:
                    raise AssertionError("Invalid write was accepted")
                cur.execute("RELEASE SAVEPOINT invalid_write")

            rejects(
                "INSERT INTO conversation_messages(id, conversation_id, position, role, content) "
                "VALUES (%s, %s, 0, 'assistant', 'duplicate')",
                (str(uuid.uuid4()), conversation), psycopg2.errors.UniqueViolation,
            )
            second = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO conversation_messages(id, conversation_id, position, role, content) "
                "VALUES (%s, %s, 1, 'user', 'follow-up')", (second, conversation),
            )
            rejects(
                "INSERT INTO conversation_jobs(id, conversation_id, user_message_id, model, revision) "
                "VALUES (%s, %s, %s, 'test-model', 0)",
                (str(uuid.uuid4()), conversation, second), psycopg2.errors.UniqueViolation,
            )
            rejects(
                "INSERT INTO conversation_jobs(id, conversation_id, user_message_id, model, revision) "
                "VALUES (%s, %s, %s, 'test-model', 0)",
                (str(uuid.uuid4()), other, second), psycopg2.errors.ForeignKeyViolation,
            )
            cur.execute("INSERT INTO images VALUES ('shared-image')")
            cur.execute("INSERT INTO conversation_message_images VALUES (%s, 'shared-image', 0)", (message,))
            cur.execute("DELETE FROM conversations WHERE id = %s", (conversation,))
            for table in ("conversation_messages", "conversation_jobs", "conversation_message_images"):
                cur.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone()[0] == 0, table
            cur.execute("SELECT count(*) FROM images")
            assert cur.fetchone()[0] == 1
        print("PASS: repeatable schema, ordering, active-job uniqueness, ownership and deletion cascades")
    finally:
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    check_schema()
