FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Run as non-root
RUN useradd -m -s /bin/bash botuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p videos && chown -R botuser:botuser /app

USER botuser

# Health check (ping endpoint)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/config')" || exit 1

CMD ["python", "main.py"]
