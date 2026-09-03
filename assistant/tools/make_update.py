#!/usr/bin/env python3
"""Small maintainer utility for building format-v1 assistant update archives."""
from __future__ import annotations
import argparse, hashlib, json, os, tarfile, tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from-version", required=True)
    p.add_argument("--to-version", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("paths", nargs="+")
    a = p.parse_args()
    root = Path.cwd().resolve()
    files = []
    with tempfile.TemporaryDirectory() as td:
        staging = Path(td)
        (staging / "files").mkdir()
        for raw in a.paths:
            src = (root / raw).resolve()
            rel = src.relative_to(root)
            if not src.is_file():
                raise SystemExit(f"Not a file: {rel}")
            dst = staging / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            mode = oct(src.stat().st_mode & 0o777)[2:].zfill(4)
            files.append({"path": rel.as_posix(), "sha256": sha256(dst), "mode": mode})
        manifest = {
            "format_version": 1,
            "project": "assistant-stack",
            "from_versions": [a.from_version],
            "to_version": a.to_version,
            "message": a.message,
            "commit_message": f"Assistant update {a.to_version}",
            "files": files,
            "delete": [],
            "deploy": [],
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        a.output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(a.output, "w:gz") as tf:
            tf.add(staging / "manifest.json", arcname="manifest.json")
            tf.add(staging / "files", arcname="files")
    print(a.output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
