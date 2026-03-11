FROM python:3.11-slim


# Tools: ffmpeg (required), curl/jq (handy for inline debugging)
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl jq \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir \
      "wheel==0.46.3" \
      "jaraco.context==6.1.1" && \
    rm -rf \
      /usr/local/lib/python3.11/site-packages/setuptools/_vendor/wheel-0.45.1.dist-info \
      /usr/local/lib/python3.11/site-packages/setuptools/_vendor/jaraco.context-5.3.0.dist-info
COPY . .
EXPOSE 9000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--reload", "--reload-dir", "/app"]
