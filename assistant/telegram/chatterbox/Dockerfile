FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_NUM_THREADS=8 \
    MKL_NUM_THREADS=8 \
    HF_HOME=/root/.cache/huggingface \
    NUMBA_CACHE_DIR=/tmp/numba-cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

RUN python -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.6.0 torchaudio==2.6.0

RUN python -m pip install --no-cache-dir \
    "git+https://github.com/resemble-ai/chatterbox.git@5de7a54aa4e5e2baadb0182dde554908b48b85c2" \
    "fastapi>=0.116,<1" \
    "uvicorn[standard]>=0.35,<1"

WORKDIR /service
COPY app.py /service/app.py

ENV CHATTERBOX_VOICE_REFERENCE=/voice/reference.wav \
    CHATTERBOX_MAX_CHARS=4000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8881", "--log-level", "warning"]
