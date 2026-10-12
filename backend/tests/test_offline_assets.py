"""The initial page must not need a CDN or a missing transitive font asset."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

PUBLIC = Path(__file__).resolve().parents[2] / "frontend/public"


def test_page_dependencies_are_local_and_present():
    class Dependencies(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            url = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
            if url:
                assert url.startswith("/") and not url.startswith("//"), url
                assert (PUBLIC / url.lstrip("/")).is_file(), url
    Dependencies().feed((PUBLIC / "index.html").read_text())


def test_vendor_content_matches_recorded_checksums():
    vendor = PUBLIC / "vendor"
    manifest = json.loads((vendor / "manifest.json").read_text())
    for name, metadata in manifest.items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == metadata["sha256"], name


def test_css_fonts_are_local_and_present():
    for file in (PUBLIC / "vendor").glob("*.css"):
        for url in re.findall(r"url\(['\"]?([^)'\"]+)", file.read_text()):
            assert not url.startswith(("http", "//")), url
            assert (file.parent / url).is_file(), url
