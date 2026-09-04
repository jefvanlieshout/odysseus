FROM python:3.13-slim

WORKDIR /app

# Keep expensive runtime dependencies independent from Brain source code.
# Normal Python edits can now reuse this cached layer.
RUN pip install --no-cache-dir \
    chromadb==1.5.9 \
    fastembed==0.8.0

COPY jarvis_brain ./jarvis_brain

RUN useradd --create-home --uid 10001 brain
USER brain

ENV BRAIN_DB_PATH=/data/brain.db
ENV BRAIN_HOST=0.0.0.0
ENV BRAIN_PORT=8765
ENV CHROMA_HOST=chromadb
ENV CHROMA_PORT=8000
ENV BRAIN_VECTOR_BACKEND=chroma
ENV BRAIN_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV BRAIN_EMBED_CACHE=/data/fastembed-cache

EXPOSE 8765
CMD ["python", "-m", "jarvis_brain.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
