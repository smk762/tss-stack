FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg=7:7.1.3-0+deb13u1 \
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

COPY app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 9001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9001"]
