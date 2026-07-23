#!/bin/sh
set -eu

umask 077

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${YYB_USERDATA:-/data}"
    chown -R app:app "${YYB_USERDATA:-/data}"
    exec gosu app "$@"
fi

exec "$@"
