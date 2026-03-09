FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg=7:7.1.3-0+deb13u1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py ./worker.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "worker.py"]
