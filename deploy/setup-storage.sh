#!/usr/bin/env bash
# ==============================================================================
# RESTAMP property-photo storage setup (Ubuntu). Idempotent, non-destructive.
#
#   sudo deploy/setup-storage.sh <SERVICE_USER> <SERVICE_GROUP> [MEDIA_ROOT]
#
# <SERVICE_USER>/<SERVICE_GROUP>: the account the backend's systemd unit runs
# as (check `systemctl show -p User,Group <unit>`); this script never creates
# users or changes group memberships. MEDIA_ROOT defaults to
# /var/www/restamp/uploads and must match RESTAMP_MEDIA_ROOT.
#
# Result:
#   MEDIA_ROOT            owner SERVICE_USER:SERVICE_GROUP, 0755
#   MEDIA_ROOT/listings   owner SERVICE_USER:SERVICE_GROUP, 0755
# Only the backend user can write; Nginx (any user) can traverse and read,
# which is intended: these are public photos served at /media/. Existing
# files are never deleted, moved or re-permissioned.
# ==============================================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] run as root (sudo)" >&2; exit 1
fi
if [[ $# -lt 2 ]]; then
    echo "usage: $0 <SERVICE_USER> <SERVICE_GROUP> [MEDIA_ROOT]" >&2; exit 1
fi
SERVICE_USER="$1"
SERVICE_GROUP="$2"
MEDIA_ROOT="${3:-/var/www/restamp/uploads}"

id -u "$SERVICE_USER" >/dev/null 2>&1 || { echo "[ERROR] user $SERVICE_USER does not exist" >&2; exit 1; }
getent group "$SERVICE_GROUP" >/dev/null || { echo "[ERROR] group $SERVICE_GROUP does not exist" >&2; exit 1; }
case "$MEDIA_ROOT" in
    /tmp*|/var/tmp*|/dev/shm*|/run*) echo "[ERROR] $MEDIA_ROOT is not persistent" >&2; exit 1 ;;
    /*) ;;
    *) echo "[ERROR] MEDIA_ROOT must be absolute" >&2; exit 1 ;;
esac

install -d -m 0755 "$(dirname "$MEDIA_ROOT")"
for dir in "$MEDIA_ROOT" "$MEDIA_ROOT/listings"; do
    if [[ -d "$dir" ]]; then
        echo "[INFO] exists: $dir (ownership/mode set on the directory only)"
    fi
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 "$dir"
done

# Verify the service user can actually write (then clean up the probe).
probe="$MEDIA_ROOT/listings/.write-probe-$$"
if sudo -u "$SERVICE_USER" sh -c "touch '$probe' && rm -f '$probe'"; then
    echo "[OK] $SERVICE_USER can write to $MEDIA_ROOT/listings"
else
    echo "[ERROR] $SERVICE_USER cannot write to $MEDIA_ROOT/listings" >&2; exit 1
fi

stat -c '%U:%G %a %n' "$MEDIA_ROOT" "$MEDIA_ROOT/listings"
df -h "$MEDIA_ROOT"
