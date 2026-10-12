FROM python:3.14-slim

LABEL maintainer="lestar"
LABEL description="Prometheus exporter for Homeflix missing media monitoring"

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Run as non-root user
RUN useradd -r -s /bin/false exporter
USER exporter

EXPOSE 9108

ENTRYPOINT ["python", "-u", "main.py"]

