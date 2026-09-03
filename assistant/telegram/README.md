# Odysseus Telegram Bridge

Standalone V1 bridge:

    Telegram -> bridge -> Odysseus /api/chat -> your selected Odysseus model

It uses Telegram long polling, so no inbound port or webhook is needed.

## 1. Create the Odysseus chat

Create a normal Odysseus chat named exactly:

    Telegram Jarvis

Select Qwen3.8-27B (or whichever model you want) and send one test message in the UI.

## 2. Configure

    cp .env.example .env
    nano .env

Fill in:

    TELEGRAM_BOT_TOKEN=...
    ODYSSEUS_API_TOKEN=ody_...

You can leave TELEGRAM_ALLOWED_USER_IDS empty for the first boot.

## 3. Check the Odysseus network

From your Odysseus checkout:

    cd ~/odysseus/odysseus
    docker inspect "$(docker compose ps -q odysseus)" \
      --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\\n"}}{{end}}'

Usually it is:

    odysseus_default

If yours differs, put the returned name in ODYSSEUS_NETWORK in `.env`.

## 4. Start

From this bridge directory:

    docker compose up -d --build

Logs:

    docker compose logs -f

## 5. Get your Telegram user ID

Message your new bot:

    /whoami

Put the returned numeric ID into `.env`:

    TELEGRAM_ALLOWED_USER_IDS=123456789

Then:

    docker compose restart

Now normal messages are accepted.

## Commands

    /start
    /whoami
    /status
    /help

## Security

- No ports are published by the bridge.
- Normal messages are allowlist-only.
- The container is non-root.
- All Linux capabilities are dropped.
- Root filesystem is read-only.
- Keep `.env` private: it contains your Telegram and Odysseus tokens.

## Troubleshooting

If session-name lookup fails, set the exact session UUID:

    ODYSSEUS_SESSION_ID=<uuid>

If you get 401/403, check that the `ody_...` token is active, has chat scope,
and belongs to the same Odysseus account that owns the Telegram Jarvis chat.

If `odysseus` does not resolve, the bridge is on the wrong Docker network.
