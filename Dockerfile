FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

RUN mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8099

# Use the Python interpreter directly instead of relying on the console-script
# launcher. This also makes the container easier to diagnose across platforms.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--proxy-headers"]
