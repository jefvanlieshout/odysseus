import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from telegram import InputFile, Update
from telegram.constants import ChatAction
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from interactions import decode_callback, interaction_keyboard, interaction_text, normalize_interaction

def _telegram_keyboard(markup):
    if markup is None:
        return None

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=button.text,
                    callback_data=button.callback_data,
                )
                for button in row
            ]
            for row in markup.inline_keyboard
        ]
    )



from voice import VoiceModeStore, normalize_voice_mode, should_synthesize

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("odysseus-telegram-bridge")
# Telegram embeds the bot token in API request URLs. Keep HTTP transport
# logging below INFO so normal logs cannot expose the token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_allowed_users(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.replace(" ", ",").split(","):
        item = item.strip()
        if not item:
            continue
        result.add(int(item))
    return result


TELEGRAM_BOT_TOKEN = env_required("TELEGRAM_BOT_TOKEN")
ODYSSEUS_API_TOKEN = env_required("ODYSSEUS_API_TOKEN")
ODYSSEUS_URL = os.getenv("ODYSSEUS_URL", "http://odysseus:7000").rstrip("/")
ODYSSEUS_SESSION_ID = os.getenv("ODYSSEUS_SESSION_ID", "").strip()
ODYSSEUS_SESSION_NAME = os.getenv("ODYSSEUS_SESSION_NAME", "Telegram Jarvis").strip()
ODYSSEUS_AGENT_MODE = env_bool("ODYSSEUS_AGENT_MODE", True)
ODYSSEUS_USE_WEB = env_bool("ODYSSEUS_USE_WEB", False)
ODYSSEUS_USE_RESEARCH = env_bool("ODYSSEUS_USE_RESEARCH", False)
ODYSSEUS_TZ_NAME = os.getenv("ODYSSEUS_TZ_NAME", "Europe/Brussels").strip()
ODYSSEUS_TIMEOUT_SECONDS = float(os.getenv("ODYSSEUS_TIMEOUT_SECONDS", "600"))
TELEGRAM_MAX_CHARS = min(int(os.getenv("TELEGRAM_MAX_CHARS", "3900")), 4096)
WHISPER_ENABLED = env_bool("WHISPER_ENABLED", True)
WHISPER_URL = os.getenv("WHISPER_URL", "http://odysseus-whisper:9000").rstrip("/")
WHISPER_TIMEOUT_SECONDS = float(os.getenv("WHISPER_TIMEOUT_SECONDS", "120"))
WHISPER_MAX_AUDIO_BYTES = int(os.getenv("WHISPER_MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))
WHISPER_MAX_VOICE_SECONDS = float(os.getenv("WHISPER_MAX_VOICE_SECONDS", "600"))
TELEGRAM_ECHO_TRANSCRIPT = env_bool("TELEGRAM_ECHO_TRANSCRIPT", False)
TTS_ENABLED = env_bool("TTS_ENABLED", True)
TTS_URL = os.getenv(
    "TTS_URL", "http://chatterbox-nano-tts:8881/v1/audio/speech"
).strip()
TTS_MODEL = os.getenv("TTS_MODEL", "chatterbox-nano").strip() or "chatterbox-nano"
TTS_VOICE = os.getenv("TTS_VOICE", "reference").strip() or "reference"
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
TTS_TIMEOUT_SECONDS = float(os.getenv("TTS_TIMEOUT_SECONDS", "120"))
TTS_MAX_CHARS = max(1, int(os.getenv("TTS_MAX_CHARS", "4000")))
TTS_MAX_AUDIO_BYTES = max(
    1, int(os.getenv("TTS_MAX_AUDIO_BYTES", str(20 * 1024 * 1024)))
)
TELEGRAM_VOICE_DEFAULT = normalize_voice_mode(
    os.getenv("TELEGRAM_VOICE_DEFAULT", "auto"), fallback="auto"
)
TELEGRAM_VOICE_STATE_FILE = os.getenv(
    "TELEGRAM_VOICE_STATE_FILE", "/state/voice_modes.json"
).strip()
ALLOWED_USERS = parse_allowed_users(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))

_http: Optional[httpx.AsyncClient] = None
_whisper_http: Optional[httpx.AsyncClient] = None
_tts_http: Optional[httpx.AsyncClient] = None
_resolved_session_id: Optional[str] = ODYSSEUS_SESSION_ID or None
_session_lock = asyncio.Lock()
_voice_modes = VoiceModeStore(
    TELEGRAM_VOICE_STATE_FILE,
    default=TELEGRAM_VOICE_DEFAULT,
)


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USERS)


def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {ODYSSEUS_API_TOKEN}",
        "Accept": "application/json",
    }


def odysseus_time_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if not ODYSSEUS_TZ_NAME:
        return headers

    headers["X-TZ-Name"] = ODYSSEUS_TZ_NAME
    try:
        # Match JavaScript Date.getTimezoneOffset(): UTC - local, in minutes.
        offset = datetime.now(ZoneInfo(ODYSSEUS_TZ_NAME)).utcoffset()
        if offset is not None:
            headers["X-TZ-Offset"] = str(-int(offset.total_seconds() // 60))
    except Exception:
        logger.warning("Could not resolve timezone %r; sending name only", ODYSSEUS_TZ_NAME)
    return headers


async def http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(ODYSSEUS_TIMEOUT_SECONDS),
            headers=auth_headers(),
        )
    return _http


async def whisper_http_client() -> httpx.AsyncClient:
    global _whisper_http
    if _whisper_http is None:
        # Keep the Odysseus bearer token out of requests to the Whisper service.
        _whisper_http = httpx.AsyncClient(timeout=httpx.Timeout(WHISPER_TIMEOUT_SECONDS))
    return _whisper_http


async def tts_http_client() -> httpx.AsyncClient:
    global _tts_http
    if _tts_http is None:
        # TTS is deliberately isolated from Odysseus auth/tool credentials.
        _tts_http = httpx.AsyncClient(timeout=httpx.Timeout(TTS_TIMEOUT_SECONDS))
    return _tts_http


def telegram_duration_seconds(value) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value)


async def whisper_transcribe(audio: bytes, filename: str = "voice.ogg", content_type: str = "audio/ogg") -> dict:
    if not WHISPER_ENABLED:
        raise RuntimeError("Voice transcription is disabled")
    if not audio:
        raise RuntimeError("Telegram returned an empty voice file")
    if len(audio) > WHISPER_MAX_AUDIO_BYTES:
        raise RuntimeError(f"Voice file is too large ({len(audio)} bytes)")

    client = await whisper_http_client()
    response = await client.post(
        f"{WHISPER_URL}/transcribe",
        files={"file": (filename, audio, content_type)},
    )
    response.raise_for_status()
    result = response.json()
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Whisper returned no transcript")
    result["text"] = text.strip()
    return result


async def synthesize_speech(text: str) -> bytes:
    if not TTS_ENABLED:
        raise RuntimeError("TTS TTS is disabled")
    clean = text.strip()
    if not clean:
        raise RuntimeError("Cannot synthesize an empty response")
    if len(clean) > TTS_MAX_CHARS:
        raise ValueError(
            f"response has {len(clean)} chars; TTS limit is {TTS_MAX_CHARS}"
        )

    client = await tts_http_client()
    response = await client.post(
        TTS_URL,
        json={
            "model": TTS_MODEL,
            "input": clean,
            "voice": TTS_VOICE,
            "response_format": "ogg",
            "speed": TTS_SPEED,
        },
        headers={"Accept": "audio/ogg"},
    )
    response.raise_for_status()
    audio = response.content
    if not audio:
        raise RuntimeError("TTS returned an empty audio response")
    if len(audio) > TTS_MAX_AUDIO_BYTES:
        raise RuntimeError(
            f"TTS audio response is too large ({len(audio)} bytes)"
        )
    return audio


async def maybe_send_voice_reply(
    update: Update,
    answer: str,
    *,
    input_was_voice: bool,
) -> None:
    # Best-effort rendering of an already-final text answer.
    if update.message is None or update.effective_chat is None:
        return

    mode = _voice_modes.get(update.effective_chat.id)
    if not TTS_ENABLED or not should_synthesize(
        mode, input_was_voice=input_was_voice
    ):
        return

    if len(answer.strip()) > TTS_MAX_CHARS:
        logger.info(
            "Skipping TTS: response chars=%d exceeds limit=%d",
            len(answer.strip()),
            TTS_MAX_CHARS,
        )
        return

    try:
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        audio = await synthesize_speech(answer)
        await update.message.reply_voice(
            voice=InputFile(audio, filename="atlas.ogg")
        )
        logger.info(
            "Sent TTS voice reply chat_id=%s mode=%s source=%s bytes=%d",
            update.effective_chat.id,
            mode,
            "voice" if input_was_voice else "text",
            len(audio),
        )
    except Exception as exc:
        # Text has already been delivered. TTS failure is intentionally non-fatal.
        logger.warning("TTS voice reply failed: %s", exc, exc_info=True)


async def resolve_session_id(force: bool = False) -> str:
    global _resolved_session_id

    if _resolved_session_id and not force:
        return _resolved_session_id

    if ODYSSEUS_SESSION_ID:
        _resolved_session_id = ODYSSEUS_SESSION_ID
        return _resolved_session_id

    if not ODYSSEUS_SESSION_NAME:
        raise RuntimeError("Set ODYSSEUS_SESSION_ID or ODYSSEUS_SESSION_NAME in .env")

    client = await http_client()
    response = await client.get(f"{ODYSSEUS_URL}/api/sessions")
    response.raise_for_status()
    sessions = response.json()

    matches = [
        s for s in sessions
        if str(s.get("name", "")).strip() == ODYSSEUS_SESSION_NAME
    ]

    if not matches:
        raise RuntimeError(
            f"No Odysseus session named {ODYSSEUS_SESSION_NAME!r} was found."
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"More than one Odysseus session is named {ODYSSEUS_SESSION_NAME!r}; "
            "set ODYSSEUS_SESSION_ID explicitly."
        )

    _resolved_session_id = str(matches[0]["id"])
    logger.info("Resolved Odysseus session %r -> %s", ODYSSEUS_SESSION_NAME, _resolved_session_id)
    return _resolved_session_id


async def _odysseus_agent_stream(message: str, session_id: str) -> tuple[int, str, str, object | None]:
    """Run the full Odysseus SSE agent loop and return status, final text, raw error body."""
    client = await http_client()
    form = {
        "message": message,
        "session": session_id,
        "mode": "agent",
        "use_web": "true" if ODYSSEUS_USE_WEB else "false",
        "use_research": "true" if ODYSSEUS_USE_RESEARCH else "false",
    }
    headers = {
        "Accept": "text/event-stream",
        **odysseus_time_headers(),
    }

    full_response: list[str] = []
    last_tool_output = ""
    interaction = None

    async with client.stream(
        "POST",
        f"{ODYSSEUS_URL}/api/chat_stream",
        data=form,
        headers=headers,
    ) as response:
        if response.status_code >= 400:
            raw = (await response.aread()).decode(errors="replace")
            return response.status_code, "", raw, None

        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue

            payload = line[6:].strip()
            if payload == "[DONE]":
                break

            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("Ignoring malformed SSE payload: %r", payload[:300])
                continue

            delta = event.get("delta")
            if isinstance(delta, str) and not event.get("thinking"):
                full_response.append(delta)

            event_type = event.get("type")
            if event_type == "tool_start":
                logger.info(
                    "Odysseus tool start: %s %s",
                    event.get("tool", "unknown"),
                    str(event.get("command", ""))[:250],
                )
            elif event_type == "tool_output":
                tool = event.get("tool", "unknown")
                output = str(event.get("output", ""))
                last_tool_output = output.strip() or last_tool_output
                logger.info(
                    "Odysseus tool output: %s exit=%s output=%s",
                    tool,
                    event.get("exit_code"),
                    output[:500].replace("\n", " "),
                )
            elif event_type in {
                "budget_exceeded",
                "rounds_exhausted",
                "loop_breaker_triggered",
                "intent_nudge_exhausted",
            }:
                logger.warning("Odysseus agent event: %s %s", event_type, event)
            elif event_type == "ask_user":
                parsed = normalize_interaction(event)
                if parsed is not None:
                    interaction = parsed
            elif event_type in {
                "approval_request",
                "approval_required",
                "confirm_action",
                "confirmation_required",
                "requires_confirmation",
            }:
                parsed = normalize_interaction(event)
                if parsed is not None:
                    interaction = parsed

    answer = "".join(full_response).strip()
    if not answer and last_tool_output and interaction is None:
        answer = last_tool_output
    if not answer and interaction is None:
        answer = "Odysseus finished the agent run but returned no text response."
    return 200, answer, "", interaction


async def odysseus_chat(message: str) -> tuple[str, object | None]:
    session_id = await resolve_session_id()

    if ODYSSEUS_AGENT_MODE:
        status, answer, raw_error, interaction = await _odysseus_agent_stream(message, session_id)
        if status == 404 and not ODYSSEUS_SESSION_ID:
            session_id = await resolve_session_id(force=True)
            status, answer, raw_error, interaction = await _odysseus_agent_stream(message, session_id)
        if status >= 400:
            request = httpx.Request("POST", f"{ODYSSEUS_URL}/api/chat_stream")
            response = httpx.Response(status, text=raw_error, request=request)
            raise httpx.HTTPStatusError(
                f"Odysseus returned HTTP {status}", request=request, response=response
            )
        return answer, interaction

    client = await http_client()
    payload = {
        "message": message,
        "session": session_id,
        "attachments": [],
        "use_web": ODYSSEUS_USE_WEB,
        "use_research": ODYSSEUS_USE_RESEARCH,
    }

    response = await client.post(
        f"{ODYSSEUS_URL}/api/chat",
        json=payload,
        headers=odysseus_time_headers(),
    )

    if response.status_code == 404 and not ODYSSEUS_SESSION_ID:
        await resolve_session_id(force=True)
        payload["session"] = _resolved_session_id
        response = await client.post(
            f"{ODYSSEUS_URL}/api/chat",
            json=payload,
            headers=odysseus_time_headers(),
        )

    response.raise_for_status()
    answer = response.json().get("response")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("Odysseus returned no text response")
    return answer.strip(), None


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return ["(empty response)"]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def interaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not is_allowed(update):
        return

    decoded = decode_callback(query.data or "")
    if decoded is None:
        await query.answer("This choice is no longer valid.")
        return

    interaction_id, index = decoded
    markup = query.message.reply_markup if query.message is not None else None
    buttons = []
    if markup is not None:
        for row in markup.inline_keyboard:
            buttons.extend(row)

    matches = [
        button for button in buttons
        if decode_callback(button.callback_data or "") == (interaction_id, index)
    ]
    if not matches:
        await query.answer("This choice is no longer valid.", show_alert=True)
        return

    selected = matches[0].text.strip()
    await query.answer(f"Selected: {selected}")

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear interaction buttons", exc_info=True)

    answer, interaction = await odysseus_chat(selected)

    if query.message is not None:
        if answer.strip():
            for chunk in split_message(answer):
                await query.message.reply_text(chunk)
        if interaction is not None:
            await query.message.reply_text(
                interaction_text(interaction),
                reply_markup=_telegram_keyboard(interaction_keyboard(interaction)),
            )


async def typing_loop(update: Update, stop: asyncio.Event) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    try:
        while not stop.is_set():
            await chat.send_action(ChatAction.TYPING)
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Typing indicator failed", exc_info=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    if not is_allowed(update):
        await update.message.reply_text(
            "Jarvis bridge is running, but you are not allowlisted yet.\n\n"
            f"Your Telegram user ID is: {user.id}\n\n"
            "Put that number in TELEGRAM_ALLOWED_USER_IDS in .env and recreate the container."
        )
        return

    mode = "agent" if ODYSSEUS_AGENT_MODE else "chat"
    await update.message.reply_text(
        f"Jarvis bridge is online in {mode} mode. Send me text or a voice note and I’ll forward it to Odysseus."
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if update.message is None or user is None:
        return

    await update.message.reply_text(
        f"Telegram user ID: {user.id}\n"
        f"Telegram chat ID: {chat.id if chat else 'unknown'}\n"
        f"Allowlisted: {'yes' if is_allowed(update) else 'no'}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not is_allowed(update):
        await update.message.reply_text("Not authorized. Use /whoami to get your user ID.")
        return

    try:
        sid = await resolve_session_id()
        client = await http_client()
        response = await client.get(f"{ODYSSEUS_URL}/api/sessions")
        response.raise_for_status()
        sessions = response.json()
        session = next((s for s in sessions if str(s.get("id")) == sid), None)

        if session:
            whisper_line = "Whisper: disabled"
            if WHISPER_ENABLED:
                try:
                    whisper_client = await whisper_http_client()
                    wr = await whisper_client.get(f"{WHISPER_URL}/health")
                    wr.raise_for_status()
                    wh = wr.json()
                    whisper_line = (
                        f"Whisper: OK ({wh.get('model', 'unknown')}, "
                        f"{wh.get('device', 'unknown')}/{wh.get('compute_type', 'unknown')})"
                    )
                except Exception as exc:
                    whisper_line = f"Whisper: ERROR ({exc})"

            await update.message.reply_text(
                "Telegram bridge: OK\n"
                "Odysseus API: OK\n"
                f"{whisper_line}\n"
                f"Session: {session.get('name', sid)}\n"
                f"Model: {session.get('model', 'unknown')}\n"
                f"Mode: {'agent' if ODYSSEUS_AGENT_MODE else 'chat'}\n"
                f"Timezone: {ODYSSEUS_TZ_NAME or 'Odysseus default'}\n"
                f"Web: {'on' if ODYSSEUS_USE_WEB else 'off'}\n"
                f"Research: {'on' if ODYSSEUS_USE_RESEARCH else 'off'}"
            )
        else:
            await update.message.reply_text(f"Bridge/API OK\nSession ID: {sid}")
    except Exception as exc:
        logger.exception("Status check failed")
        await update.message.reply_text(f"Odysseus status check failed: {exc}")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return
    if not is_allowed(update):
        await update.message.reply_text("Not authorized. Use /whoami to get your user ID.")
        return

    chat_id = update.effective_chat.id
    args = list(context.args or [])
    if not args:
        mode = _voice_modes.get(chat_id)
        await update.message.reply_text(
            f"Voice replies: {mode}\n"
            "auto = voice only after a voice note\n"
            "on = voice after text and voice input\n"
            "off = text only"
        )
        return

    if len(args) != 1:
        await update.message.reply_text("Usage: /voice [auto|on|off]")
        return

    try:
        mode = _voice_modes.set(chat_id, args[0])
    except ValueError:
        await update.message.reply_text("Usage: /voice [auto|on|off]")
        return
    except OSError as exc:
        logger.exception("Could not persist Telegram voice mode")
        await update.message.reply_text(f"Could not save the voice preference: {exc}")
        return

    await update.message.reply_text(f"Voice replies set to: {mode}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "/start - bridge status/setup hint\n"
        "/whoami - show your Telegram IDs\n"
        "/status - check Odysseus session/model/mode\n"
        "/voice [auto|on|off] - control TTS voice replies\n"
        "/help - show help\n\n"
        "Normal text and Telegram voice notes are sent to Odysseus. Voice notes are transcribed locally first. "
        "Voice replies are rendered only after the final text response; text remains the primary response. "
        "In agent mode, Odysseus can use its enabled tools."
    )


async def run_prompt_and_reply(
    update: Update,
    text: str,
    *,
    input_was_voice: bool = False,
) -> None:
    if update.message is None:
        return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(typing_loop(update, stop_typing))
    try:
        answer, interaction = await odysseus_chat(text)
        if interaction is not None:
            await update.effective_message.reply_text(
                interaction_text(interaction),
                reply_markup=_telegram_keyboard(interaction_keyboard(interaction)),
            )
    except httpx.HTTPStatusError as exc:
        logger.exception(
            "Odysseus HTTP error status=%s body=%r",
            exc.response.status_code,
            exc.response.text[:500],
        )
        await update.message.reply_text(
            f"Odysseus returned HTTP {exc.response.status_code}. Check the bridge logs."
        )
        return
    except Exception as exc:
        logger.exception("Odysseus request failed")
        await update.message.reply_text(f"I couldn't reach/use Odysseus: {exc}")
        return
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    for chunk in split_message(answer):
        await update.message.reply_text(chunk)

    # Voice is downstream and best-effort; the complete text is already sent.
    await maybe_send_voice_reply(
        update, answer, input_was_voice=input_was_voice
    )


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.voice is None:
        return

    if not is_allowed(update):
        user = update.effective_user
        logger.warning(
            "Blocked Telegram voice message from user_id=%s chat_id=%s",
            getattr(user, "id", None),
            getattr(update.effective_chat, "id", None),
        )
        await update.message.reply_text("Not authorized. Use /whoami to get your user ID.")
        return

    if not WHISPER_ENABLED:
        await update.message.reply_text("Voice transcription is disabled on this bridge.")
        return

    voice = update.message.voice
    duration = telegram_duration_seconds(voice.duration)
    if WHISPER_MAX_VOICE_SECONDS > 0 and duration > WHISPER_MAX_VOICE_SECONDS:
        await update.message.reply_text(
            f"That voice note is too long ({duration:.0f}s). "
            f"Current limit is {WHISPER_MAX_VOICE_SECONDS:.0f}s."
        )
        return

    if voice.file_size and voice.file_size > WHISPER_MAX_AUDIO_BYTES:
        await update.message.reply_text(
            f"That voice note is too large ({voice.file_size} bytes)."
        )
        return

    async with _session_lock:
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(typing_loop(update, stop_typing))
        try:
            tg_file = await voice.get_file()
            audio = bytes(await tg_file.download_as_bytearray())
            result = await whisper_transcribe(
                audio,
                filename="telegram-voice.ogg",
                content_type="audio/ogg",
            )
            transcript = result["text"]
            logger.info(
                "Voice transcription language=%s probability=%s chars=%d",
                result.get("language"),
                result.get("language_probability"),
                len(transcript),
            )
            logger.debug("Voice transcript: %s", transcript)

            if TELEGRAM_ECHO_TRANSCRIPT:
                await update.message.reply_text(f"🎤 Heard: {transcript}")

            # The transcript enters the exact same Odysseus agent path as typed text.
            answer, interaction = await odysseus_chat(transcript)
            if interaction is not None:
                await update.effective_message.reply_text(
                    interaction_text(interaction),
                    reply_markup=_telegram_keyboard(interaction_keyboard(interaction)),
                )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            logger.exception(
                "Voice/Odysseus HTTP error status=%s body=%r",
                exc.response.status_code if exc.response is not None else "unknown",
                body,
            )
            if exc.request.url.host == httpx.URL(WHISPER_URL).host:
                await update.message.reply_text(
                    f"Whisper returned HTTP {exc.response.status_code}. Check the bridge/Whisper logs."
                )
            else:
                await update.message.reply_text(
                    f"Odysseus returned HTTP {exc.response.status_code}. Check the bridge logs."
                )
            return
        except Exception as exc:
            logger.exception("Voice processing failed")
            await update.message.reply_text(f"I couldn't process that voice note: {exc}")
            return
        finally:
            stop_typing.set()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        for chunk in split_message(answer):
            await update.message.reply_text(chunk)

        # In auto mode this is the path that speaks back.
        await maybe_send_voice_reply(update, answer, input_was_voice=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return

    if not is_allowed(update):
        user = update.effective_user
        logger.warning(
            "Blocked Telegram message from user_id=%s chat_id=%s",
            getattr(user, "id", None),
            getattr(update.effective_chat, "id", None),
        )
        await update.message.reply_text("Not authorized. Use /whoami to get your user ID.")
        return

    text = update.message.text.strip()
    if not text:
        return

    async with _session_lock:
        await run_prompt_and_reply(update, text)


async def post_init(application: Application) -> None:
    logger.info(
        "Telegram bridge starting; Odysseus URL=%s mode=%s timezone=%s Whisper=%s TTS=%s voice_default=%s",
        ODYSSEUS_URL,
        "agent" if ODYSSEUS_AGENT_MODE else "chat",
        ODYSSEUS_TZ_NAME or "default",
        WHISPER_URL if WHISPER_ENABLED else "disabled",
        TTS_URL if TTS_ENABLED else "disabled",
        TELEGRAM_VOICE_DEFAULT,
    )
    if ALLOWED_USERS:
        logger.info("Allowed Telegram user IDs: %s", sorted(ALLOWED_USERS))
    else:
        logger.warning("No Telegram users allowlisted. Use /whoami, update .env, then recreate.")

    try:
        await resolve_session_id()
    except Exception as exc:
        logger.warning("Initial Odysseus session resolution failed: %s", exc)


async def post_shutdown(application: Application) -> None:
    global _http, _whisper_http, _tts_http
    if _http is not None:
        await _http.aclose()
        _http = None
    if _whisper_http is not None:
        await _whisper_http.aclose()
        _whisper_http = None
    if _tts_http is not None:
        await _tts_http.aclose()
        _tts_http = None


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(interaction_callback, pattern=r"^odyask:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
