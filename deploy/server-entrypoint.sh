#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
    chown -R 10001:10001 /data
    chmod 0700 /data
    exec setpriv \
        --reuid=10001 \
        --regid=10001 \
        --clear-groups \
        --bounding-set=-all \
        --inh-caps=-all \
        --ambient-caps=-all \
        "$@"
fi

exec "$@"
