#!/bin/sh
# Claim the data volume, then hand off to the app as an unprivileged user.
#
# The image builds /data/cache and gives it to `terminal`, but that only ever
# describes the container layer. Mounting anything there — a Fly volume, a
# compose bind mount — shadows it with a filesystem owned by root and empty of
# the cache directory, and the app dies on its first write. The fix has to run
# after the mount exists, which means here rather than in the Dockerfile.
set -e

DATA_DIR="${MFT_DATA_ROOT:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR/cache"
    chown -R terminal:terminal "$DATA_DIR"

    # Drop root for the actual process. setpriv ships with util-linux, which is
    # an essential package on Debian, so the fallback should stay unused.
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=terminal --regid=terminal --init-groups "$@"
    fi
    exec su terminal -s /bin/sh -c 'exec "$@"' -- sh "$@"
fi

# Already unprivileged (docker run --user, or a re-exec): nothing to hand off.
exec "$@"
