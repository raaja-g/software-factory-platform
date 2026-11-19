#!/bin/sh
# Waits for the sidecar's rendered config, starts Verdaccio and restarts it when the config changes.
# (Restart, not reload: Verdaccio rereads its uplink credentials only at start; M0 S13.i must confirm.)
set -eu
SRC=/pkg-auth/npm.yaml
DST=/tmp/verdaccio.yaml
until [ -f /pkg-auth/ready ] && [ -f "$SRC" ]; do sleep 1; done
cp "$SRC" "$DST"
sum=$(cksum < "$SRC")
verdaccio --config "$DST" &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; exit 0' TERM INT
while sleep 10; do
  if ! kill -0 "$pid" 2>/dev/null; then wait "$pid" || true; exit 1; fi
  now=$(cksum < "$SRC" 2>/dev/null || echo "$sum")
  if [ "$now" != "$sum" ]; then
    cp "$SRC" "$DST"; sum=$now
    kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
    verdaccio --config "$DST" &
    pid=$!
  fi
done
