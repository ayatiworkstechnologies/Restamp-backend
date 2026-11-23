#!/usr/bin/env bash
# ==============================================================================
# RESTAMP Local Photo Storage Backup & Restore Script
# Canonical Storage Root: /var/lib/restamp/property-photos/
# ==============================================================================
set -euo pipefail

STORAGE_ROOT="/var/lib/restamp/property-photos"
BACKUP_DIR="/var/backups/restamp/photos"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

usage() {
    echo "Usage: $0 {backup|restore <archive_file>|verify <archive_file>}"
    exit 1
}

if [[ $# -lt 1 ]]; then
    usage
fi

ACTION="$1"

case "$ACTION" in
    backup)
        echo "=== Starting Photo Storage Backup ==="
        mkdir -p "$BACKUP_DIR"
        ARCHIVE_PATH="$BACKUP_DIR/property_photos_${TIMESTAMP}.tar.gz"
        CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"

        echo "[INFO] Creating compressed archive: $ARCHIVE_PATH..."
        # Backup using tar with atomic temporary file
        tar -czf "${ARCHIVE_PATH}.tmp" -C "$STORAGE_ROOT" listings
        mv "${ARCHIVE_PATH}.tmp" "$ARCHIVE_PATH"

        echo "[INFO] Generating SHA-256 checksum..."
        sha256sum "$ARCHIVE_PATH" | awk '{print $1}' > "$CHECKSUM_PATH"

        SIZE_MB=$(du -m "$ARCHIVE_PATH" | cut -f1)
        echo "[SUCCESS] Backup completed successfully. Size: ${SIZE_MB} MB"
        echo "[INFO] Archive: $ARCHIVE_PATH"
        echo "[INFO] Checksum: $(cat "$CHECKSUM_PATH")"

        # Retain last 14 backups, remove older archives
        echo "[INFO] Cleaning up archives older than 14 days..."
        find "$BACKUP_DIR" -name "property_photos_*.tar.gz*" -mtime +14 -delete
        ;;

    verify)
        if [[ $# -lt 2 ]]; then
            echo "[ERROR] Archive file path required for verification." >&2
            usage
        fi
        ARCHIVE="$2"
        CHECKSUM_FILE="${ARCHIVE}.sha256"

        if [[ ! -f "$ARCHIVE" ]]; then
            echo "[ERROR] Archive file does not exist: $ARCHIVE" >&2
            exit 1
        fi

        if [[ -f "$CHECKSUM_FILE" ]]; then
            EXPECTED=$(cat "$CHECKSUM_FILE")
            ACTUAL=$(sha256sum "$ARCHIVE" | awk '{print $1}')
            if [[ "$EXPECTED" != "$ACTUAL" ]]; then
                echo "[ERROR] Checksum mismatch!" >&2
                echo "Expected: $EXPECTED" >&2
                echo "Actual:   $ACTUAL" >&2
                exit 1
            fi
            echo "[INFO] Checksum verified: $ACTUAL"
        fi

        echo "[INFO] Testing archive integrity with tar..."
        tar -tzf "$ARCHIVE" > /dev/null
        echo "[SUCCESS] Archive integrity verified: $ARCHIVE"
        ;;

    restore)
        if [[ $# -lt 2 ]]; then
            echo "[ERROR] Archive file path required for restore." >&2
            usage
        fi
        ARCHIVE="$2"

        if [[ ! -f "$ARCHIVE" ]]; then
            echo "[ERROR] Archive file does not exist: $ARCHIVE" >&2
            exit 1
        fi

        echo "=== RESTORE WARNING ==="
        echo "This operation will extract images from $ARCHIVE into $STORAGE_ROOT."
        read -r -p "Are you sure you want to proceed? [y/N]: " CONFIRM
        if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
            echo "[INFO] Restore aborted by user."
            exit 0
        fi

        echo "[INFO] Extracting archive..."
        tar -xzf "$ARCHIVE" -C "$STORAGE_ROOT"

        echo "[INFO] Resetting directory permissions..."
        chown -R restamp:restamp "$STORAGE_ROOT"
        chmod 0750 "$STORAGE_ROOT" "$STORAGE_ROOT/listings"
        find "$STORAGE_ROOT" -type d -exec chmod 0750 {} +
        find "$STORAGE_ROOT" -type f -exec chmod 0644 {} +

        echo "[SUCCESS] Restore completed successfully."
        ;;

    *)
        usage
        ;;
esac
