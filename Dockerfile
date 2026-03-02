FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY config/ ./config/
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY ui/ ./ui/

RUN mkdir -p /app/data

# Use startup script
CMD ["./scripts/startup.sh", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
