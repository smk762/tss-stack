FROM python:3.11-slim


# Tools: ffmpeg (required), curl/jq (handy for inline debugging)
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl jq \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 9000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--reload", "--reload-dir", "/app"]
