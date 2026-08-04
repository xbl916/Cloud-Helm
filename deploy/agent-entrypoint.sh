#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
    chown -R 0:0 /data
    chmod 0700 /data
    exec setpriv \
        --bounding-set=-all \
        --inh-caps=-all \
        --ambient-caps=-all \
        "$@"
fi

exec "$@"
