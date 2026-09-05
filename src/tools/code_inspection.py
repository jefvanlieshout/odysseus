from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict

from src.tools._common import _parse_tool_args

_DEFAULT_ROOT = "/workspace/odysseus"
_MAX_READ_BYTES = 512 * 1024
_MAX_SEARCH_FILE_BYTES = 512 * 1024
_MAX_DIFF_CHARS = 24000

_DENIED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", "data", "logs", ".local",
}
_DENIED_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", "authorized_keys",
}
_DENIED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx",
    ".crt", ".cer", ".der",
}

def _repo_root() -> Path:
    return Path(os.environ.get("ODYSSEUS_SELF_CODE_ROOT") or _DEFAULT_ROOT).resolve()

def _deny_reason(path: Path, root: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return "path escapes the self-code repository"
    parts = tuple(part.casefold() for part in rel.parts)
    if any(part in _DENIED_DIRS for part in parts):
        return "path is in a blocked private/runtime directory"
    name = path.name.casefold()
    if name in _DENIED_FILENAMES or name.startswith(".env."):
        return "environment/credential files are blocked"
    if path.suffix.casefold() in _DENIED_SUFFIXES:
        return "database/key/certificate files are blocked"
    return None

def _resolve_path(raw: Any, *, must_exist: bool = True) -> tuple[Path, Path]:
    root = _repo_root()
    if not root.exists() or not root.is_dir():
        raise ValueError(
            f"self-code repository is unavailable at {root}; "
            "mount the real repository read-only into the Odysseus container"
        )
    text = str(raw or ".").strip() or "."
    candidate = Path(text)
    if candidate.is_absolute():
        raise ValueError("absolute paths are not allowed; use a repository-relative path")
    resolved = (root / candidate).resolve()
    reason = _deny_reason(resolved, root)
    if reason:
        raise ValueError(reason)
    if must_exist and not resolved.exists():
        raise ValueError(f"repository path does not exist: {text}")
    return root, resolved

def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            sample = fh.read(4096)
    except OSError:
        return True
    return b"\x00" in sample

def _safe_file(path: Path, root: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if _deny_reason(path.resolve(), root):
        return False
    try:
        if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
            return False
    except OSError:
        return False
    return not _looks_binary(path)

def _git(root: Path, *args: str) -> str:
    cmd = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    completed = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=10, check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise ValueError(detail[:800])
    return completed.stdout

def _status(root: Path) -> Dict[str, Any]:
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    porcelain = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    dirty_lines = [line for line in porcelain.splitlines() if line.strip()]
    return {
        "repository_root": str(root),
        "branch": branch,
        "head": head,
        "dirty": bool(dirty_lines),
        "dirty_entry_count": len(dirty_lines),
        "access": "read-only",
    }

def _tree(root: Path, base: Path, *, depth: int, limit: int) -> Dict[str, Any]:
    depth = max(0, min(int(depth), 5))
    limit = max(1, min(int(limit), 400))
    lines: list[str] = []
    base_depth = len(base.relative_to(root).parts)
    def visit(directory: Path) -> None:
        if len(lines) >= limit:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            return
        for child in entries:
            if len(lines) >= limit:
                return
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if child.is_symlink() or _deny_reason(resolved, root):
                continue
            rel = resolved.relative_to(root)
            current_depth = len(rel.parts) - base_depth
            if current_depth < 1 or current_depth > depth + 1:
                continue
            prefix = "  " * (current_depth - 1)
            lines.append(f"{prefix}{rel.as_posix()}{'/' if child.is_dir() else ''}")
            if child.is_dir() and current_depth <= depth:
                visit(child)
    if base.is_file():
        lines.append(base.relative_to(root).as_posix())
    else:
        visit(base)
    return {
        "path": base.relative_to(root).as_posix() or ".",
        "entries": lines,
        "truncated": len(lines) >= limit,
    }

def _read(root: Path, path: Path, *, start_line: int, end_line: int | None) -> Dict[str, Any]:
    if not path.is_file():
        raise ValueError("read requires a file path")
    if path.is_symlink():
        raise ValueError("symlink targets are not readable through inspect_code")
    size = path.stat().st_size
    if size > _MAX_READ_BYTES:
        raise ValueError(f"file is too large to read safely ({size} bytes)")
    if _looks_binary(path):
        raise ValueError("binary files are not readable through inspect_code")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    if end_line is None:
        end = min(len(lines), start + 199)
    else:
        end = max(start, min(int(end_line), start + 399, len(lines)))
    selected = lines[start - 1:end]
    numbered = "\n".join(f"{idx:6d}  {line}" for idx, line in enumerate(selected, start=start))
    return {
        "path": path.relative_to(root).as_posix(),
        "start_line": start, "end_line": end, "total_lines": len(lines),
        "content": numbered,
    }

def _iter_files(root: Path, base: Path):
    if base.is_file():
        if _safe_file(base, root):
            yield base
        return
    for current, dirs, files in os.walk(base):
        current_path = Path(current)
        kept_dirs = []
        for name in dirs:
            child = current_path / name
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if child.is_symlink() or _deny_reason(resolved, root):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            path = current_path / name
            if _safe_file(path, root):
                yield path

def _search(root: Path, base: Path, *, query: str, regex: bool,
            case_sensitive: bool, limit: int) -> Dict[str, Any]:
    query = str(query or "")
    if not query:
        raise ValueError("search requires a non-empty query")
    limit = max(1, min(int(limit), 200))
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = None
    if regex:
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    scanned = 0
    for path in _iter_files(root, base):
        scanned += 1
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            matched = bool(pattern.search(line)) if pattern else (
                needle in (line if case_sensitive else line.casefold())
            )
            if not matched:
                continue
            matches.append({
                "path": path.relative_to(root).as_posix(),
                "line": line_no,
                "text": line[:300],
            })
            if len(matches) >= limit:
                return {
                    "query": query, "matches": matches, "truncated": True,
                    "files_scanned": scanned,
                }
    return {
        "query": query, "matches": matches, "truncated": False,
        "files_scanned": scanned,
    }

def _diff(root: Path, path: Path | None, *, staged: bool) -> Dict[str, Any]:
    args = ["diff", "--no-ext-diff", "--unified=3"]
    if staged:
        args.append("--cached")
    if path is not None:
        args.extend(["--", path.relative_to(root).as_posix()])
    text = _git(root, *args)
    truncated = len(text) > _MAX_DIFF_CHARS
    if truncated:
        text = text[:_MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return {"staged": bool(staged), "diff": text, "truncated": truncated}

async def do_inspect_code(content: str, owner: str | None = None) -> Dict[str, Any]:
    """Read-only inspection of the real Odysseus/Gwen repository."""
    del owner
    try:
        args = _parse_tool_args(content) if str(content or "").strip() else {}
        action = str(args.get("action") or "status").strip().casefold()
        root = _repo_root()
        if action == "status":
            root, _ = _resolve_path(".", must_exist=True)
            result = _status(root)
        elif action == "tree":
            root, base = _resolve_path(args.get("path") or ".", must_exist=True)
            result = _tree(root, base, depth=args.get("depth", 2), limit=args.get("limit", 200))
        elif action == "read":
            root, path = _resolve_path(args.get("path"), must_exist=True)
            result = _read(root, path, start_line=args.get("start_line", 1), end_line=args.get("end_line"))
        elif action == "search":
            root, base = _resolve_path(args.get("path") or ".", must_exist=True)
            result = _search(
                root, base, query=str(args.get("query") or ""),
                regex=bool(args.get("regex", False)),
                case_sensitive=bool(args.get("case_sensitive", False)),
                limit=args.get("limit", 80),
            )
        elif action == "diff":
            path = None
            if args.get("path"):
                root, path = _resolve_path(args.get("path"), must_exist=True)
            else:
                root, _ = _resolve_path(".", must_exist=True)
            result = _diff(root, path, staged=bool(args.get("staged", False)))
        else:
            return {"error": "Unknown inspect_code action. Use: status, tree, search, read, diff", "exit_code": 1}
        return {"output": json.dumps(result, ensure_ascii=False, indent=2), **result, "exit_code": 0}
    except Exception as exc:
        return {"error": f"inspect_code failed: {exc}", "exit_code": 1}
