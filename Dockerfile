FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       fonts-dejavu-core \
       build-essential \
       python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Install greenlet explicitly first (critical for SQLAlchemy async)
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install --no-cache-dir 'greenlet>=3.0.0,<4.0.0'

# Install all remaining dependencies
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x run_server.sh

# Verify greenlet is actually installed
RUN python -c "import greenlet; print(f'✓ greenlet {greenlet.__version__} installed')"

CMD ["python", "main.py"]
