"""Short-lived browser credentials for direct Soniox audio streaming."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import jsonify, request


def enabled():
    return bool(os.environ.get("SONIOX_API_KEY", "").strip())


def register_routes(app, require_auth):
    @app.post("/api/dictation/session")
    @require_auth
    def create_dictation_session():
        # Cross-origin forms cannot set this header; no CORS access is granted.
        if request.headers.get("X-Brief-Dictation") != "1":
            return jsonify(error="invalid_request"), 400
        key = os.environ.get("SONIOX_API_KEY", "").strip()
        if not key:
            return jsonify(error="dictation_unavailable"), 503
        upstream = Request(
            "https://api.soniox.com/v1/auth/temporary-api-key",
            data=json.dumps({
                "usage_type": "transcribe_websocket",
                "expires_in_seconds": 60,
                "single_use": True,
            }).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(upstream, timeout=10) as response:
                result = json.loads(response.read(16384))
            token = result.get("api_key")
            if not isinstance(token, str) or not token or token == key:
                raise ValueError("Invalid temporary credential")
        except HTTPError as exc:
            error = "dictation_balance_exhausted" if exc.code == 402 else "dictation_provider_unavailable"
            return jsonify(error=error), 503
        except (URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError):
            return jsonify(error="dictation_provider_unavailable"), 503
        response = jsonify(api_key=token, expires_at=result.get("expires_at"))
        response.headers["Cache-Control"] = "no-store"
        return response
