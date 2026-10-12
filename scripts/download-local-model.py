#!/usr/bin/env python3
"""One-time download; pinned content hash, atomic install, standard library only."""
import hashlib
from pathlib import Path
import urllib.request

NAME = "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
SHA256 = "cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046"
URL = "https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/" + NAME


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    directory = Path(__file__).resolve().parents[1] / "models"
    directory.mkdir(exist_ok=True)
    target = directory / NAME
    if target.exists():
        if digest(target) != SHA256:
            raise SystemExit("Existing model checksum differs; move it aside before retrying.")
        print("Model already present; checksum verified.")
        return
    partial = target.with_suffix(".partial")
    print("Downloading 1.12 GB of model weights…", flush=True)
    with urllib.request.urlopen(URL, timeout=60) as response, partial.open("wb") as file:
        while block := response.read(1024 * 1024):
            file.write(block)
    if digest(partial) != SHA256:
        raise SystemExit("Checksum mismatch. Partial file retained for inspection; model not installed.")
    partial.chmod(0o644)
    partial.replace(target)
    print(f"Verified: {target}")


if __name__ == "__main__":
    main()
