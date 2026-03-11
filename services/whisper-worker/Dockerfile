FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir \
      "wheel==0.46.3" \
      "jaraco.context==6.1.1" && \
    rm -rf \
      /usr/local/lib/python3.11/site-packages/setuptools/_vendor/wheel-0.45.1.dist-info \
      /usr/local/lib/python3.11/site-packages/setuptools/_vendor/jaraco.context-5.3.0.dist-info

COPY worker.py ./worker.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "worker.py"]