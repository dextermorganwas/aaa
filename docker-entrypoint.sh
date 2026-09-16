#!/bin/sh
set -eu

mkdir -p /data /data/images
if ! su -s /bin/sh appuser -c 'test -w /data && test -w /data/images'; then
    chown -R appuser:appuser /data
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8099}"
echo "Starting Stremio Art Proxy on ${HOST}:${PORT}"
exec su -s /bin/sh appuser -c "exec python -m uvicorn app.main:app --host '${HOST}' --port '${PORT}' --proxy-headers"
