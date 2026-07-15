FROM python:3.11-slim

# Install system dependencies and force clean pip cache
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/.cache/pip/*

WORKDIR /app

COPY requirements.txt .
# Install python-multipart with fresh pip, then install all requirements
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --no-deps python-multipart==0.0.6 && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
