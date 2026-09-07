# Odysseus Telegram Bridge

Telegram text/voice -> bridge -> local Whisper -> Odysseus -> text reply
-> optional local Chatterbox Nano voice reply.

The bridge uses Telegram long polling, so no inbound port or webhook is needed.

## Voice

Commands:

    /voice
    /voice auto
    /voice on
    /voice off

- `auto`: voice-in gets text + voice-out; typed input gets text only.
- `on`: text + voice for successful replies.
- `off`: text only.

Outgoing voice replies use CPU-only Chatterbox Nano and Telegram-native
OGG/Opus. TTS failure never suppresses the text reply.

The reference WAV is intentionally not committed. Set its local path in `.env`:

    TTS_VOICE_REFERENCE_PATH=/absolute/path/to/atlas-reference.wav

## Start

    docker compose up -d --build

Containers:

- `odysseus-telegram-bridge`
- `odysseus-chatterbox-nano-tts`

Chatterbox Nano is capped at 8 CPUs / 6 GB RAM and receives no GPU.

## Security

- No bridge/TTS ports are published.
- Telegram messages are allowlist-only.
- The bridge is non-root with a read-only root filesystem.
- The voice reference is mounted read-only.
- Odysseus credentials are not sent to Whisper/TTS.
- `httpx` and `httpcore` INFO logs are suppressed because Telegram embeds the
  bot token in API request URLs.
- Keep `.env` private.

## Troubleshooting

    docker compose ps
    docker compose logs --tail=100 telegram-bridge chatterbox-nano-tts
