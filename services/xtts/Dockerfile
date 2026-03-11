FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1

# hadolint ignore=DL3008
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential \
      ffmpeg \
      libportaudio2 \
      libportaudiocpp0 \
      portaudio19-dev \
      git && \
    rm -rf /var/lib/apt/lists/*

# hadolint ignore=DL3013
RUN pip install --no-cache-dir --upgrade pip 'setuptools<81' wheel==0.46.3 && \
    pip install --no-cache-dir \
      xtts-api-server==0.8.6 \
      TTS==0.21.3 \
      uvicorn==0.41.0 && \
    pip install --no-cache-dir \
      Brotli==1.2.0 \
      requests==2.32.5 \
      urllib3==2.6.3 \
      cryptography==46.0.5 \
      nltk==3.9.3 \
      pillow==12.1.1 \
      transformers==4.48.0

WORKDIR /app
RUN mkdir -p /app/example

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8020
ENTRYPOINT ["/entrypoint.sh"]
