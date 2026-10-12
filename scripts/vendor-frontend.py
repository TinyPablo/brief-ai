#!/usr/bin/env python3
"""Explicit dependency refresh. Normal builds serve the checked-in assets offline."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1] / "frontend/public/vendor"
SOURCES = {
    "react.js": "https://cdn.jsdelivr.net/npm/react@18.3.1/umd/react.production.min.js",
    "react-dom.js": "https://cdn.jsdelivr.net/npm/react-dom@18.3.1/umd/react-dom.production.min.js",
    "babel.js": "https://cdn.jsdelivr.net/npm/@babel/standalone@7.26.9/babel.min.js",
    "tailwind.js": "https://cdn.tailwindcss.com/3.4.17",
    "marked.js": "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
    "purify.js": "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js",
    "highlight.js": "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js",
    "highlight.css": "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark.min.css",
    "katex.js": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js",
    "katex.css": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
    "marked-katex.js": "https://cdn.jsdelivr.net/npm/marked-katex-extension@5.1.4/lib/index.umd.js",
}
for name, package in {
    "react": "react@18.3.1", "react-dom": "react-dom@18.3.1",
    "babel": "@babel/standalone@7.26.9", "tailwind": "tailwindcss@3.4.17",
    "marked": "marked@12.0.2", "purify": "dompurify@3.1.6",
    "highlight": "highlight.js@11.9.0", "katex": "katex@0.16.11",
    "marked-katex": "marked-katex-extension@5.1.4",
}.items():
    SOURCES[f"licenses/{name}.txt"] = f"https://cdn.jsdelivr.net/npm/{package}/LICENSE" + (".md" if name == "marked" else "")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    def fetch(name, url):
        print(name, flush=True)
        data = subprocess.check_output(["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "60", url])
        path = ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest[name] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
        return data
    for name, url in SOURCES.items():
        fetch(name, url)
    css = (ROOT / "katex.css").read_text()
    for name in sorted(set(re.findall(r"url\((fonts/[^)]+)\)", css))):
        fetch(name, "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/" + name)
    # Retain the existing fonts with local, pinned fontsource distributions.
    font_css = []
    for family, version, weights in [("space-grotesk", "5.1.1", [400, 500, 600, 700]), ("space-mono", "5.1.1", [400, 700])]:
        for weight in weights:
            name = f"fonts/{family}-latin-{weight}-normal.woff2"
            fetch(name, f"https://cdn.jsdelivr.net/npm/@fontsource/{family}@{version}/files/{family}-latin-{weight}-normal.woff2")
            label = family.replace("-", " ").title()
            font_css.append(f"@font-face{{font-family:'{label}';font-style:normal;font-weight:{weight};font-display:swap;src:url('{name}') format('woff2')}}")
        fetch(f"licenses/{family}.txt", f"https://cdn.jsdelivr.net/npm/@fontsource/{family}@{version}/LICENSE")
    (ROOT / "fonts.css").write_text("\n".join(font_css) + "\n")
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
