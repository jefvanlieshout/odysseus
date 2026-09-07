from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final


VOICE_MODES: Final[frozenset[str]] = frozenset({"auto", "on", "off"})


def normalize_voice_mode(value: str | None, *, fallback: str | None = None) -> str:
    mode = (value or "").strip().lower()
    if mode in VOICE_MODES:
        return mode
    if fallback is not None:
        fallback_mode = fallback.strip().lower()
        if fallback_mode not in VOICE_MODES:
            raise ValueError(f"Invalid fallback voice mode: {fallback!r}")
        return fallback_mode
    raise ValueError(f"Invalid voice mode: {value!r}; expected auto, on, or off")


def should_synthesize(mode: str, *, input_was_voice: bool) -> bool:
    mode = normalize_voice_mode(mode)
    if mode == "on":
        return True
    if mode == "off":
        return False
    return input_was_voice


class VoiceModeStore:
    # Small per-chat preference store with atomic JSON writes.

    def __init__(self, path: str | os.PathLike[str], *, default: str = "auto") -> None:
        self.path = Path(path)
        self.default = normalize_voice_mode(default)
        self._modes: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            # A bad preference file must never stop the Telegram bridge.
            return

        if not isinstance(raw, dict):
            return

        loaded: dict[str, str] = {}
        for chat_id, mode in raw.items():
            try:
                loaded[str(chat_id)] = normalize_voice_mode(str(mode))
            except ValueError:
                continue
        self._modes = loaded

    def get(self, chat_id: int | str) -> str:
        return self._modes.get(str(chat_id), self.default)

    def set(self, chat_id: int | str, mode: str) -> str:
        normalized = normalize_voice_mode(mode)
        self._modes[str(chat_id)] = normalized
        self._save()
        return normalized

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        payload = json.dumps(self._modes, indent=2, sort_keys=True) + "\n"
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
