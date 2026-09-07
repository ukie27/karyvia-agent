#!/bin/sh
# 容器数据目录与宿主机统一使用 `~/.karyvia/`。
dir="$HOME/.karyvia"

# Drop privileges whenever the container starts as root. Mounted data
# directories may be root-owned, and a plain `docker run` defaults to root.
# Chown the data dir so the non-root user can write it, then re-exec as karyvia.
# Fail closed if privilege dropping is unavailable.
if [ "$(id -u)" = "0" ]; then
    chown -R karyvia:karyvia "$dir" 2>/dev/null || echo "[entrypoint] warning: chown $dir failed"
    if setpriv --reuid=karyvia --regid=karyvia --init-groups true 2>/dev/null; then
        echo "[entrypoint] dropping privileges to karyvia via setpriv"
        exec setpriv --reuid=karyvia --regid=karyvia --init-groups karyvia "$@"
    fi
    echo "[entrypoint] error: started as root but setpriv privilege drop failed — refusing to run as root" >&2
    exit 1
fi

# Already non-root: make sure the data dir is writable before starting.
if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    owner_uid=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null)
    cat >&2 <<EOF
Error: $dir is not writable (owned by UID $owner_uid, running as UID $(id -u)).

Fix (pick one):
  Host:   sudo chown -R 1000:1000 ~/.karyvia
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
EOF
    exit 1
fi

exec karyvia "$@"
