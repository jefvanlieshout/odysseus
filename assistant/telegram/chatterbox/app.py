from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

import torchaudio as ta
from chatterbox.tts_turbo import ChatterboxTurboTTS
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

VOICE_REFERENCE = Path(os.getenv("CHATTERBOX_VOICE_REFERENCE", "/voice/reference.wav"))
MAX_CHARS = max(1, int(os.getenv("CHATTERBOX_MAX_CHARS", "4000")))

if not VOICE_REFERENCE.is_file():
    raise RuntimeError(f"Voice reference not found: {VOICE_REFERENCE}")

app = FastAPI(title="Odysseus Chatterbox Nano TTS")
_model_lock = threading.Lock()

print("Loading Chatterbox Nano on CPU...", flush=True)
model = ChatterboxTurboTTS.from_pretrained(device="cpu", nano=True)
print(f"Chatterbox Nano ready; voice={VOICE_REFERENCE}", flush=True)

class SpeechRequest(BaseModel):
    model: str | None = None
    input: str
    voice: str | None = None
    response_format: str = "ogg"
    speed: float = 1.0

@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "backend": "chatterbox-nano",
        "device": "cpu",
        "voice_reference": VOICE_REFERENCE.name,
    }

@app.post("/v1/audio/speech")
def speech(request: SpeechRequest) -> Response:
    text = request.input.strip()
    if not text:
        raise HTTPException(status_code=400, detail="input must not be empty")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=413, detail="input exceeds configured limit")
    if request.response_format.lower() != "ogg":
        raise HTTPException(status_code=400, detail="only response_format=ogg is supported")
    if abs(request.speed - 1.0) > 0.001:
        raise HTTPException(status_code=400, detail="backend currently uses native speed=1.0")

    with _model_lock:
        wav = model.generate(text, audio_prompt_path=str(VOICE_REFERENCE))

    with tempfile.TemporaryDirectory(prefix="atlas-tts-") as tmp:
        wav_path = Path(tmp) / "speech.wav"
        ogg_path = Path(tmp) / "speech.ogg"
        ta.save(str(wav_path), wav, model.sr)

        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(wav_path),
                "-ar", "48000",
                "-ac", "1",
                "-c:a", "libopus",
                "-b:a", "48k",
                "-vbr", "on",
                "-application", "voip",
                str(ogg_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        audio = ogg_path.read_bytes()

    if not audio:
        raise HTTPException(status_code=500, detail="TTS produced empty audio")

    return Response(
        content=audio,
        media_type="audio/ogg",
        headers={"Content-Length": str(len(audio))},
    )
