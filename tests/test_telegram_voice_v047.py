from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "assistant" / "telegram" / "voice.py"

spec = importlib.util.spec_from_file_location("telegram_voice_v047", MODULE_PATH)
assert spec and spec.loader
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_normalize_voice_mode_accepts_supported_modes(mode):
    assert voice.normalize_voice_mode(mode.upper()) == mode


def test_normalize_voice_mode_rejects_unknown_mode():
    with pytest.raises(ValueError):
        voice.normalize_voice_mode("sometimes")


@pytest.mark.parametrize(
    ("mode", "input_was_voice", "expected"),
    [
        ("auto", False, False),
        ("auto", True, True),
        ("on", False, True),
        ("on", True, True),
        ("off", False, False),
        ("off", True, False),
    ],
)
def test_should_synthesize(mode, input_was_voice, expected):
    assert voice.should_synthesize(mode, input_was_voice=input_was_voice) is expected


def test_voice_mode_store_defaults_and_persists(tmp_path):
    path = tmp_path / "voice_modes.json"

    first = voice.VoiceModeStore(path, default="auto")
    assert first.get(123) == "auto"

    assert first.set(123, "on") == "on"
    assert first.get(123) == "on"

    second = voice.VoiceModeStore(path, default="auto")
    assert second.get(123) == "on"
    assert second.get(456) == "auto"


def test_bad_state_file_falls_back_to_default(tmp_path):
    path = tmp_path / "voice_modes.json"
    path.write_text("{not-json", encoding="utf-8")

    store = voice.VoiceModeStore(path, default="auto")
    assert store.get(123) == "auto"
