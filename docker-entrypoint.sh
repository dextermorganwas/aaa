#!/bin/sh
set -eu

# /data is normally a host bind mount, so its host permissions replace the
# ownership baked into the image. Only repair ownership when the app user
# cannot write, avoiding a recursive chown on every container restart.
mkdir -p /data /data/images
if ! su -s /bin/sh appuser -c 'test -w /data && test -w /data/images'; then
    chown -R appuser:appuser /data
fi

exec su -s /bin/sh appuser -c 'exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --proxy-headers'
