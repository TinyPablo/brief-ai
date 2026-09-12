import base64

import pytest

from app import (
    MAX_IMAGES,
    MAX_IMAGE_MB,
    MAX_TOTAL_IMAGE_MB,
    validate_images,
)


def _b64(n_bytes):
    """A base64 blob that decodes to exactly n_bytes of raw data."""
    return base64.b64encode(b"x" * n_bytes).decode()


def _img(n_bytes=10, media_type="image/png"):
    return {"media_type": media_type, "data": _b64(n_bytes)}


def test_no_images_is_a_noop():
    error, decoded = validate_images(None)
    assert error is None
    assert decoded == []

    error, decoded = validate_images([])
    assert error is None
    assert decoded == []


def test_within_limits_decodes_raw_bytes():
    error, decoded = validate_images([_img(10), _img(20)])
    assert error is None
    assert len(decoded) == 2
    assert decoded[0]["raw"] == b"x" * 10
    assert decoded[1]["raw"] == b"x" * 20
    # original base64 "data" is preserved too, for providers that want it as-is
    assert decoded[0]["media_type"] == "image/png"


def test_too_many_images_rejected():
    error, decoded = validate_images([_img(10) for _ in range(MAX_IMAGES + 1)])
    assert error is not None
    assert error[0] == "too_many_images"
    assert decoded is None


def test_at_max_images_is_allowed():
    error, decoded = validate_images([_img(10) for _ in range(MAX_IMAGES)])
    assert error is None
    assert len(decoded) == MAX_IMAGES


def test_oversized_single_image_rejected():
    too_big = MAX_IMAGE_MB * 1_000_000 + 1
    error, decoded = validate_images([_img(too_big)])
    assert error is not None
    assert error[0] == "image_too_large"
    assert decoded is None


def test_total_size_over_cap_rejected():
    # Three images, each safely under the per-image cap, whose sum exceeds the total cap.
    per_image = MAX_IMAGE_MB * 1_000_000 - 1_000_000
    assert per_image < MAX_IMAGE_MB * 1_000_000  # sanity: not tripping the per-image check
    assert per_image * 3 > MAX_TOTAL_IMAGE_MB * 1_000_000  # sanity: does trip the total cap
    error, decoded = validate_images([_img(per_image), _img(per_image), _img(per_image)])
    assert error is not None
    assert error[0] == "images_too_large_total"
    assert decoded is None


def test_unsupported_media_type_rejected():
    error, decoded = validate_images([_img(10, media_type="image/svg+xml")])
    assert error is not None
    assert error[0] == "unsupported_image_type"
    assert decoded is None


def test_invalid_base64_rejected():
    error, decoded = validate_images([{"media_type": "image/png", "data": "not-base64!!"}])
    assert error is not None
    assert error[0] == "invalid_images"
    assert decoded is None


def test_non_list_payload_rejected():
    error, decoded = validate_images({"not": "a list"})
    assert error is not None
    assert error[0] == "invalid_images"
    assert decoded is None
