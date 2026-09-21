"""Attachment persistence: what validate_images derives, what store_images
writes, and when delete_orphan_images is allowed to drop bytes."""

import base64
import hashlib

import pytest

import app as appmod
from app import (
    MAX_THUMB_BYTES,
    delete_orphan_images,
    store_images,
    validate_images,
)

PNG = "image/png"


def _b64(raw):
    return base64.b64encode(raw).decode()


def _entry(raw=b"pixels", **extra):
    entry = {"media_type": PNG, "data": _b64(raw)}
    entry.update(extra)
    return entry


class _FakeCursor:
    """Records every statement so a test can assert on what was written."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def statements(self, needle):
        return [c for c in self.calls if needle in c[0]]


# --- sha256 and dimensions -------------------------------------------------

def test_sha256_is_derived_from_the_decoded_bytes():
    _, decoded = validate_images([_entry(b"pixels")])
    assert decoded[0]["sha256"] == hashlib.sha256(b"pixels").hexdigest()


def test_identical_bytes_share_one_storage_key():
    """This is what makes deduplication work: the same file attached twice
    resolves to the same primary key."""
    _, decoded = validate_images([_entry(b"same"), _entry(b"same")])
    assert decoded[0]["sha256"] == decoded[1]["sha256"]


def test_unusable_dimensions_become_zero_rather_than_an_error():
    """Dimensions are layout hints from the client, never used for decoding -
    a bad one must not fail an otherwise valid upload."""
    _, decoded = validate_images([_entry(width="huge", height=-5)])
    assert decoded[0]["width"] == 0
    assert decoded[0]["height"] == 0


def test_plausible_dimensions_are_kept():
    _, decoded = validate_images([_entry(width=2048, height=1536)])
    assert (decoded[0]["width"], decoded[0]["height"]) == (2048, 1536)


# --- thumbnails ------------------------------------------------------------

def test_thumbnail_is_optional():
    error, decoded = validate_images([_entry()])
    assert error is None
    assert decoded[0]["thumb"] is None
    assert decoded[0]["thumb_media_type"] is None


def test_valid_thumbnail_is_decoded():
    error, decoded = validate_images([
        _entry(thumb=_b64(b"small"), thumb_media_type="image/webp"),
    ])
    assert error is None
    assert decoded[0]["thumb"] == b"small"
    assert decoded[0]["thumb_media_type"] == "image/webp"


def test_thumbnail_type_is_restricted():
    error, decoded = validate_images([
        _entry(thumb=_b64(b"x"), thumb_media_type="image/svg+xml"),
    ])
    assert decoded is None
    assert error[0] == "unsupported_image_type"


def test_oversized_thumbnail_is_refused():
    error, decoded = validate_images([
        _entry(thumb=_b64(b"x" * (MAX_THUMB_BYTES + 1)), thumb_media_type="image/webp"),
    ])
    assert decoded is None
    assert error[0] == "image_too_large"


def test_thumbnail_must_be_valid_base64():
    error, decoded = validate_images([
        _entry(thumb="not base64!", thumb_media_type="image/webp"),
    ])
    assert decoded is None
    assert error[0] == "invalid_images"


# --- writing ---------------------------------------------------------------

def test_store_images_writes_one_image_row_and_one_join_row_each():
    _, decoded = validate_images([_entry(b"a"), _entry(b"b")])
    cur = _FakeCursor()
    store_images(cur, 42, decoded)

    assert len(cur.statements("INSERT INTO images")) == 2
    joins = cur.statements("INSERT INTO prompt_images")
    assert [params[2] for _, params in joins] == [0, 1]
    assert all(params[0] == 42 for _, params in joins)


def test_image_insert_is_idempotent_on_the_content_hash():
    """Re-attaching a known file must cost a join row, not a second copy of
    the bytes."""
    _, decoded = validate_images([_entry(b"a")])
    cur = _FakeCursor()
    store_images(cur, 1, decoded)
    sql = cur.statements("INSERT INTO images")[0][0]
    assert "ON CONFLICT (sha256) DO UPDATE" in sql


def test_existing_thumbnail_is_never_overwritten_only_backfilled():
    _, decoded = validate_images([_entry(b"a")])
    cur = _FakeCursor()
    store_images(cur, 1, decoded)
    sql = cur.statements("INSERT INTO images")[0][0]
    assert "COALESCE(images.thumb, EXCLUDED.thumb)" in sql


def test_storing_no_images_touches_nothing():
    cur = _FakeCursor()
    store_images(cur, 1, [])
    assert cur.calls == []


# --- deleting --------------------------------------------------------------

def test_orphan_sweep_is_skipped_when_there_is_nothing_to_sweep():
    cur = _FakeCursor()
    delete_orphan_images(cur, set())
    assert cur.calls == []


def test_orphan_sweep_spares_images_another_prompt_still_uses():
    """Deduplication means a deleted prompt's image may still belong to
    someone else's row, so the delete is conditional, not a plain DELETE."""
    cur = _FakeCursor()
    delete_orphan_images(cur, {"a" * 64})
    sql, params = cur.statements("DELETE FROM images")[0]
    assert "NOT EXISTS" in sql
    assert "FROM prompt_images" in sql
    assert params == (["a" * 64],)


# --- serving ---------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/images/{}",
    "/api/images/{}/thumb",
])
def test_image_routes_require_auth(path):
    client = appmod.app.test_client()
    res = client.get(path.format("a" * 64))
    assert res.status_code == 401


@pytest.mark.parametrize("sha", ["short", "z" * 64, "A" * 64, "../../etc/passwd"])
def test_non_hex_shas_are_rejected_before_touching_the_database(sha, monkeypatch):
    def explode():
        raise AssertionError("the database must not be reached for a malformed sha")

    monkeypatch.setattr(appmod, "get_db", explode)
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["auth"] = True
    assert client.get(f"/api/images/{sha}").status_code == 404


def test_thumbnails_have_an_aggregate_ceiling():
    """Individually-legal thumbnails must not be able to add up past the
    request body limit."""
    big = _b64(b"x" * (appmod.MAX_THUMB_BYTES - 1))
    entries = [
        _entry(bytes([i]), thumb=big, thumb_media_type="image/webp")
        for i in range(appmod.MAX_IMAGES)
    ]
    error, decoded = validate_images(entries)
    assert decoded is None
    assert error[0] == "images_too_large_total"
    assert "Thumbnails" in error[1]


class _StubImageConn:
    """A database holding exactly one image row."""

    def __init__(self, row):
        self.row = row

    def cursor(self, *a, **k):
        return self

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _get(path, row, monkeypatch):
    monkeypatch.setattr(appmod, "get_db", lambda: _StubImageConn(row))
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["auth"] = True
    return client.get(path)


SHA = "a" * 64
WITH_THUMB = (b"original-bytes", "image/png", b"thumb-bytes", "image/webp")
WITHOUT_THUMB = (b"original-bytes", "image/png", None, None)


def test_full_route_serves_the_original(monkeypatch):
    res = _get(f"/api/images/{SHA}", WITH_THUMB, monkeypatch)
    assert res.status_code == 200
    assert res.data == b"original-bytes"
    assert res.mimetype == "image/png"


def test_thumb_route_serves_the_thumbnail(monkeypatch):
    res = _get(f"/api/images/{SHA}/thumb", WITH_THUMB, monkeypatch)
    assert res.data == b"thumb-bytes"
    assert res.mimetype == "image/webp"


def test_thumb_route_falls_back_to_the_original_when_none_was_stored(monkeypatch):
    """Images uploaded before thumbnails existed, and small ones the browser
    saw no point shrinking, still have to render."""
    res = _get(f"/api/images/{SHA}/thumb", WITHOUT_THUMB, monkeypatch)
    assert res.status_code == 200
    assert res.data == b"original-bytes"
    assert res.mimetype == "image/png"


def test_missing_image_is_a_404(monkeypatch):
    res = _get(f"/api/images/{SHA}", None, monkeypatch)
    assert res.status_code == 404


def test_bytes_are_cached_forever_but_never_by_a_shared_cache(monkeypatch):
    """Content-addressed, so immutable is honest - but the route is behind
    auth, so the response must not land in a proxy cache."""
    res = _get(f"/api/images/{SHA}", WITH_THUMB, monkeypatch)
    assert "immutable" in res.headers["Cache-Control"]
    assert "private" in res.headers["Cache-Control"]
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_full_and_thumb_do_not_share_an_etag(monkeypatch):
    """Same sha, two different payloads - one ETag would let a cache serve the
    thumbnail where the original was asked for."""
    full = _get(f"/api/images/{SHA}", WITH_THUMB, monkeypatch).headers["ETag"]
    thumb = _get(f"/api/images/{SHA}/thumb", WITH_THUMB, monkeypatch).headers["ETag"]
    assert full != thumb
