#!/bin/sh
set -e
# New files (SQLite db, WAL, caches) readable by the app user only.
umask 077
# Bind-mounting ./data over /data hides the image's ownership. Fix it, then drop root.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  chown -R app:app /data
  chmod 700 /data
  if command -v runuser >/dev/null 2>&1; then
    exec runuser -u app -- "$@"
  fi
  exec su -s /bin/sh app -c 'exec "$0" "$@"' -- "$@"
fi
exec "$@"
