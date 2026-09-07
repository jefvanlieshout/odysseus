import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "assistant" / "telegram" / "bot.py"
COMPOSE = ROOT / "assistant" / "telegram" / "compose.yaml"
APP = ROOT / "assistant" / "telegram" / "chatterbox" / "app.py"
ENV = ROOT / "assistant" / "telegram" / ".env.example"

def test_generic_tts_and_ogg_contract():
    bot = BOT.read_text(encoding="utf-8")
    assert "KOKORO_TTS_" not in bot
    assert re.search(r'os\.getenv\(\s*"TTS_URL"', bot)
    assert '"response_format": "ogg"' in bot
    assert '"Accept": "audio/ogg"' in bot
    assert 'filename="atlas.ogg"' in bot

def test_transport_logs_do_not_expose_telegram_token_urls():
    bot = BOT.read_text(encoding="utf-8")
    assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in bot
    assert 'logging.getLogger("httpcore").setLevel(logging.WARNING)' in bot

def test_cpu_chatterbox_uses_telegram_native_opus():
    app = APP.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    assert 'from_pretrained(device="cpu", nano=True)' in app
    assert '"libopus"' in app
    assert 'media_type="audio/ogg"' in app
    assert "chatterbox-nano-tts:" in compose
    assert "kokoro-tts:" not in compose
    assert "TTS_VOICE_REFERENCE_PATH" in compose

def test_example_does_not_commit_personal_voice_path():
    env = ENV.read_text(encoding="utf-8")
    assert "TTS_VOICE_REFERENCE_PATH=/absolute/path/to/atlas-reference.wav" in env
    assert "/home/jef/" not in env
