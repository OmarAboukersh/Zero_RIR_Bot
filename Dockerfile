# Zero RIR web app.
# The image holds only the code; your data (workouts.csv, models/, settings, .env)
# lives in a folder mounted at /data, so rebuilding the image never touches it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DATA_DIR=/data \
    APP_PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY web ./web

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/ping')"

CMD ["python", "web_app.py"]
