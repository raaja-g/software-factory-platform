#!/bin/sh
# Waits for the sidecar's rendered routes, starts the PyPI proxy and signals it (SIGHUP reloads the
# routes) when the file changes. Uploads are never served: the proxy answers 405 to every non-GET.
set -eu
SRC=/pkg-auth/pypi.json
DST=/tmp/pypi.json
until [ -f /pkg-auth/ready ] && [ -f "$SRC" ]; do sleep 1; done
cp "$SRC" "$DST"
sum=$(cksum < "$SRC")
python /opt/pkg-mirror/pypi_proxy.py "$DST" &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; exit 0' TERM INT
while sleep 10; do
  if ! kill -0 "$pid" 2>/dev/null; then wait "$pid" || true; exit 1; fi
  now=$(cksum < "$SRC" 2>/dev/null || echo "$sum")
  if [ "$now" != "$sum" ]; then
    cp "$SRC" "$DST"; sum=$now
    kill -HUP "$pid"
  fi
done
