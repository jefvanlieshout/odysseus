#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from urllib.request import urlopen

PROJECT = "assistant-stack"
FORMAT_VERSION = 1
VERSION_FILE = "ASSISTANT_VERSION"

DENY_PARTS = {
    "brain-data",
    "data",
    "secrets",
    "private",
    "__pycache__",
}
DENY_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx"}


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def git(repo: Path, *args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo, capture=capture, check=check)


def commit(repo: Path, message: str) -> None:
    name = git(repo, "config", "user.name", capture=True, check=False).stdout.strip()
    email = git(repo, "config", "user.email", capture=True, check=False).stdout.strip()
    cmd = ["git"]
    if not name:
        cmd += ["-c", "user.name=Assistant Updater"]
    if not email:
        cmd += ["-c", "user.email=assistant-updater@local"]
    cmd += ["commit", "-m", message]
    run(cmd, cwd=repo)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(raw: str) -> Path:
    p = PurePosixPath(raw)
    if p.is_absolute() or not p.parts or any(part in {"", ".", ".."} for part in p.parts):
        raise ValueError(f"Unsafe update path: {raw!r}")
    return Path(*p.parts)


def is_secret_path(rel: Path) -> bool:
    parts = set(rel.parts)
    name = rel.name.lower()
    if parts & DENY_PARTS:
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if rel.suffix.lower() in DENY_SUFFIXES:
        return True
    return False


def safe_extract(package: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with tarfile.open(package, "r:gz") as tf:
        members = tf.getmembers()
        for member in members:
            rel = safe_rel(member.name)
            target = (dest / rel).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise ValueError(f"Archive escapes extraction directory: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"Links/devices are not allowed in updates: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Unsupported archive entry: {member.name}")

        # Extract manually instead of tarfile.extractall so behavior is stable
        # across Python versions and archive metadata cannot create surprises.
        for member in members:
            rel = safe_rel(member.name)
            target = dest / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                raise ValueError(f"Could not read archive member: {member.name}")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


def load_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        raise ValueError("Update archive has no manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"Unsupported update format: {data.get('format_version')!r}")
    if data.get("project") != PROJECT:
        raise ValueError(f"Wrong project: {data.get('project')!r}")
    if not isinstance(data.get("to_version"), str) or not data["to_version"].strip():
        raise ValueError("Manifest has no valid to_version")
    if not isinstance(data.get("files", []), list) or not isinstance(data.get("delete", []), list):
        raise ValueError("Manifest files/delete fields must be lists")
    return data


def verify_payload(extracted: Path, manifest: dict) -> list[tuple[Path, Path, int | None]]:
    verified: list[tuple[Path, Path, int | None]] = []
    seen: set[Path] = set()
    for item in manifest.get("files", []):
        if not isinstance(item, dict):
            raise ValueError("Each files entry must be an object")
        rel = safe_rel(str(item.get("path", "")))
        if rel in seen:
            raise ValueError(f"Duplicate file in manifest: {rel}")
        seen.add(rel)
        if is_secret_path(rel):
            raise ValueError(f"Update packages may not overwrite secret/runtime data: {rel}")
        src = extracted / "files" / rel
        if not src.is_file():
            raise ValueError(f"Payload file missing: {rel}")
        expected = str(item.get("sha256", "")).lower()
        actual = sha256_file(src)
        if expected != actual:
            raise ValueError(f"SHA256 mismatch for {rel}: expected {expected}, got {actual}")
        mode = item.get("mode")
        parsed_mode: int | None = None
        if mode is not None:
            parsed_mode = int(str(mode), 8)
            if parsed_mode & ~0o777:
                raise ValueError(f"Unsafe mode for {rel}: {mode}")
        verified.append((rel, src, parsed_mode))

    for raw in manifest.get("delete", []):
        rel = safe_rel(str(raw))
        if is_secret_path(rel):
            raise ValueError(f"Update packages may not delete secret/runtime data: {rel}")
    return verified


def staged_paths(repo: Path) -> list[Path]:
    out = git(repo, "diff", "--cached", "--name-only", "-z", capture=True).stdout
    return [Path(p) for p in out.split("\0") if p]


def validate_staged(repo: Path) -> None:
    bad = [p for p in staged_paths(repo) if is_secret_path(p)]
    if bad:
        git(repo, "reset", check=False)
        listing = "\n  - ".join(str(p) for p in bad)
        raise RuntimeError(
            "Refusing to commit secret/runtime-looking files. Add them to .gitignore first:\n  - " + listing
        )

    for rel in staged_paths(repo):
        path = repo / rel
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "-----BEGIN PRIVATE KEY-----" in text or "-----BEGIN OPENSSH PRIVATE KEY-----" in text:
            git(repo, "reset", check=False)
            raise RuntimeError(f"Refusing to commit a private key found in {rel}")


def backup_dirty_tree(repo: Path, target_version: str) -> None:
    status = git(repo, "status", "--porcelain=v1", capture=True).stdout
    if not status.strip():
        return
    print("Working tree has changes; saving them as a local Git backup commit...")
    git(repo, "add", "-A")
    validate_staged(repo)
    if staged_paths(repo):
        commit(repo, f"Local backup before assistant update {target_version}")


def make_pre_tag(repo: Path, version: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"assistant-pre-{version}-{stamp}"
    git(repo, "tag", tag)
    return tag


def copy_atomic(src: Path, dst: Path, mode: int | None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=str(dst.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(src, temp)
        if mode is not None:
            temp.chmod(mode)
        os.replace(temp, dst)
    finally:
        temp.unlink(missing_ok=True)


def syntax_checks(repo: Path, touched: list[Path]) -> None:
    py_files = [str(repo / p) for p in touched if p.suffix == ".py" and (repo / p).exists()]
    for path in py_files:
        run([sys.executable, "-m", "py_compile", path])

    for rel in touched:
        path = repo / rel
        if path.suffix == ".sh" and path.exists():
            run(["bash", "-n", str(path)])
        if path.suffix == ".json" and path.exists():
            json.loads(path.read_text(encoding="utf-8"))
        if path.suffix == ".toml" and path.exists():
            try:
                import tomllib
            except ImportError:
                continue
            tomllib.loads(path.read_text(encoding="utf-8"))


def run_deploy(repo: Path, deploy: list[dict]) -> None:
    for step in deploy:
        if not isinstance(step, dict):
            raise ValueError("deploy entries must be objects")
        kind = step.get("type")
        if kind == "docker_compose_up":
            cmd = ["docker", "compose"]
            for raw in step.get("files", []):
                rel = safe_rel(str(raw))
                cmd += ["-f", str(repo / rel)]
            cmd += ["up", "-d"]
            if step.get("build", True):
                cmd.append("--build")
            services = step.get("services", [])
            if services:
                cmd += [str(s) for s in services]
            print("Deploying with Docker Compose...")
            run(cmd, cwd=repo)
        elif kind == "http_health":
            url = str(step.get("url", ""))
            if not url.startswith(("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")):
                raise ValueError("Health checks are restricted to localhost URLs")
            timeout = float(step.get("timeout_seconds", 10))
            print(f"Checking {url} ...")
            with urlopen(url, timeout=timeout) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Health check failed with HTTP {response.status}")
        else:
            raise ValueError(f"Unsupported deploy step: {kind!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a self-contained assistant update package safely.")
    parser.add_argument("package", type=Path, help="assistant-update-*.tar.gz")
    parser.add_argument("--no-deploy", action="store_true", help="Apply/commit code but skip declared deployment steps")
    args = parser.parse_args()

    package = args.package.expanduser().resolve()
    if not package.is_file():
        print(f"Update package not found: {package}", file=sys.stderr)
        return 2

    try:
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel", capture=True).stdout.strip()).resolve()
    except Exception:
        print("Run this updater from inside the assistant/Odysseus Git repository.", file=sys.stderr)
        return 2

    version_path = repo / VERSION_FILE
    if not version_path.is_file():
        print(f"Missing {VERSION_FILE}; bootstrap the repository first.", file=sys.stderr)
        return 2
    current_version = version_path.read_text(encoding="utf-8").strip()

    try:
        with tempfile.TemporaryDirectory(prefix="assistant-update-") as tmp:
            extracted = Path(tmp)
            safe_extract(package, extracted)
            manifest = load_manifest(extracted)
            target_version = manifest["to_version"].strip()
            allowed = manifest.get("from_versions", [])
            if allowed != "*" and current_version not in allowed:
                raise RuntimeError(
                    f"This update expects one of {allowed!r}; installed version is {current_version!r}."
                )
            verified = verify_payload(extracted, manifest)

            print(f"Assistant update: {current_version} -> {target_version}")
            print(manifest.get("message", "(no description)"))
            print()

            backup_dirty_tree(repo, target_version)
            pre_commit = git(repo, "rev-parse", "HEAD", capture=True).stdout.strip()
            pre_tag = make_pre_tag(repo, target_version)

            touched: list[Path] = []
            backup_root = extracted / "rollback"
            existing: dict[Path, bool] = {}

            try:
                for rel, src, mode in verified:
                    dst = repo / rel
                    existing[rel] = dst.exists()
                    if dst.exists() and dst.is_file():
                        backup = backup_root / rel
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dst, backup)
                    copy_atomic(src, dst, mode)
                    touched.append(rel)

                for raw in manifest.get("delete", []):
                    rel = safe_rel(str(raw))
                    dst = repo / rel
                    existing[rel] = dst.exists()
                    if dst.exists():
                        if not dst.is_file():
                            raise RuntimeError(f"Refusing to delete non-file path: {rel}")
                        backup = backup_root / rel
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dst, backup)
                        dst.unlink()
                    touched.append(rel)

                version_path.write_text(target_version + "\n", encoding="utf-8")
                touched.append(Path(VERSION_FILE))
                syntax_checks(repo, touched)

                git(repo, "add", "-A")
                validate_staged(repo)
                commit(repo, manifest.get("commit_message") or f"Assistant update {target_version}")
                version_tag = f"assistant-v{target_version}"
                if git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{version_tag}", check=False).returncode != 0:
                    git(repo, "tag", version_tag)

            except Exception:
                print("Update failed before deployment; restoring previous files...", file=sys.stderr)
                git(repo, "reset", "--hard", pre_commit, check=False)
                for rel, was_existing in existing.items():
                    dst = repo / rel
                    if not was_existing and dst.exists() and dst.is_file():
                        dst.unlink(missing_ok=True)
                raise

            deploy = manifest.get("deploy", [])
            if deploy and not args.no_deploy:
                run_deploy(repo, deploy)
            elif deploy:
                print("Deployment steps were skipped (--no-deploy).")

            print()
            print(f"OK: assistant is now at version {target_version}")
            print(f"Git safety tag: {pre_tag}")
            print("Git history contains the update; no numbered backup source files were created.")
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
