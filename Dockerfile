FROM python:3.11-slim

# Cache bust for clean build - 2026-07-15
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Force pip upgrade and explicit python-multipart install to prevent caching
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir python-multipart==0.0.6 && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
