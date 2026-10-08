FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Layer 1 — OS packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    zstd \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Layer 2 — Ollama binary (~150 MB; cached until install script changes)
# Pinned to 0.12.0: on our A40 vGPU hosts (driver 535), newer Ollama releases
# either refuse the GPU outright (0.30.11+: "driver too old", needs 550+) or
# detect it but crash on actual inference (0.30.8/0.30.10: "CUDA error:
# device kernel image is invalid" — a vGPU-specific kernel compat issue, not
# just a driver-version check). 0.12.0 is the newest release confirmed to run
# real end-to-end inference on this hardware. Re-verify end-to-end (not just
# GPU detection) with `ollama run` before bumping this.
ENV OLLAMA_VERSION=0.12.0
RUN curl -fsSL https://ollama.com/install.sh | sh

# Layer 3 — Python dependencies (cached until requirements.txt changes)
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

# Layer 4 — Application code (most frequently changed; always last)
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY prompts/ ./prompts/
RUN chmod +x /app/scripts/entrypoint.sh

# Declare mount points so Docker creates them with correct ownership
RUN mkdir -p /shared /root/.ollama/models

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
