#!/usr/bin/env bash
# ==============================================================================
# RESTAMP property-photo backup / off-site copy / verify / restore.
#
#   backup               archive MEDIA_ROOT/listings + sha256 + manifest
#   offsite <archive>    copy archive (+ .sha256, .manifest) to $OFFSITE_TARGET
#   verify  <archive>    checksum + tar integrity
#   restore <archive>    extract into a staging dir, then copy ONLY files that
#                        are missing in MEDIA_ROOT (never overwrites/deletes)
#
# Configuration (environment; nothing secret is stored in this script):
#   MEDIA_ROOT       default /var/www/restamp/uploads
#   BACKUP_DIR       default /var/backups/restamp/photos   (LOCAL staging only)
#   OFFSITE_TARGET   REQUIRED for `offsite`: an rsync destination on ANOTHER
#                    machine/provider, e.g. backup@<HOST>:/srv/restamp-photos/
#                    (SSH key auth), or use `rclone copy` to object storage
#                    instead — see deploy/README.md §6.
#   SERVICE_USER / SERVICE_GROUP   owner for restored files (restore only)
#   KEEP_DAYS        local retention, default 7
#
# A local archive on the same disk is NOT a backup by itself; run `offsite`
# after every `backup` (the cron example in the README does both).
# ==============================================================================
set -euo pipefail

MEDIA_ROOT="${MEDIA_ROOT:-/var/www/restamp/uploads}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/restamp/photos}"
KEEP_DAYS="${KEEP_DAYS:-7}"

usage() { echo "usage: $0 {backup|offsite <archive>|verify <archive>|restore <archive>}" >&2; exit 1; }
[[ $# -ge 1 ]] || usage

case "$1" in
backup)
    install -d -m 0750 "$BACKUP_DIR"
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    archive="$BACKUP_DIR/property_photos_${ts}.tar.gz"
    tar -czf "$archive.tmp" -C "$MEDIA_ROOT" listings
    mv "$archive.tmp" "$archive"
    sha256sum "$archive" | awk '{print $1}' > "$archive.sha256"
    # Manifest: relative path + size, used to compare against property_media
    # rows (public_id = local:<listing_id>/<uuid>.<ext>) after a restore.
    (cd "$MEDIA_ROOT" && find listings -type f ! -name '.*' -printf '%P %s\n' | sort) > "$archive.manifest"
    echo "[OK] $archive ($(du -h "$archive" | cut -f1), $(wc -l < "$archive.manifest") files)"
    find "$BACKUP_DIR" -name 'property_photos_*' -mtime +"$KEEP_DAYS" -delete
    echo "$archive"
    ;;
offsite)
    [[ $# -ge 2 && -f "$2" ]] || usage
    : "${OFFSITE_TARGET:?OFFSITE_TARGET is not set (must be another machine/provider)}"
    rsync -a --partial "$2" "$2.sha256" "$2.manifest" "$OFFSITE_TARGET"
    echo "[OK] copied $(basename "$2") to off-site target"
    ;;
verify)
    [[ $# -ge 2 && -f "$2" ]] || usage
    expected="$(cat "$2.sha256")"
    actual="$(sha256sum "$2" | awk '{print $1}')"
    [[ "$expected" == "$actual" ]] || { echo "[ERROR] checksum mismatch" >&2; exit 1; }
    tar -tzf "$2" > /dev/null
    echo "[OK] checksum and archive integrity verified"
    ;;
restore)
    [[ $# -ge 2 && -f "$2" ]] || usage
    : "${SERVICE_USER:?set SERVICE_USER}" "${SERVICE_GROUP:?set SERVICE_GROUP}"
    "$0" verify "$2"
    staging="$(mktemp -d "${BACKUP_DIR}/restore.XXXXXX")"
    tar -xzf "$2" -C "$staging" --no-same-owner
    # Copy only missing files; existing (possibly newer) files are untouched.
    rsync -a --ignore-existing --chown="$SERVICE_USER:$SERVICE_GROUP" \
          --chmod=D0755,F0644 "$staging/listings/" "$MEDIA_ROOT/listings/"
    rm -rf "$staging"
    echo "[OK] restore finished (missing files copied; nothing overwritten or deleted)"
    ;;
*) usage ;;
esac
