FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential \
      ffmpeg \
      libportaudio2 \
      libportaudiocpp0 \
      portaudio19-dev \
      git && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
      transformers==4.37.2 \
      xtts-api-server==0.8.0 \
      TTS==0.21.3 \
      uvicorn==0.25.0

WORKDIR /app
RUN mkdir -p /app/example

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8020
ENTRYPOINT ["/entrypoint.sh"]
