#!/usr/bin/env bash
# ==============================================================================
# RESTAMP Production Storage Initialization Script (Ubuntu)
# Canonical Storage Root: /var/lib/restamp/property-photos/
# ==============================================================================
set -euo pipefail

STORAGE_ROOT="/var/lib/restamp/property-photos"
APP_USER="restamp"
APP_GROUP="restamp"
WEB_USER="www-data"

echo "=== Initializing RESTAMP Property Photo Storage ==="

# 1. Verify root privileges
if [[ $EUID -ne 0 ]]; then
   echo "[ERROR] This script must be run as root (sudo)." >&2
   exit 1
fi

# 2. Ensure application user and group exist
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    echo "[INFO] Creating service user '$APP_USER'..."
    useradd --system --user-group --shell /bin/false "$APP_USER"
fi

# 3. Add www-data to restamp group so Nginx can read stored images
echo "[INFO] Adding '$WEB_USER' to group '$APP_GROUP' for static file read access..."
usermod -aG "$APP_GROUP" "$WEB_USER"

# 4. Create storage directory hierarchy
echo "[INFO] Creating directory hierarchy under $STORAGE_ROOT..."
mkdir -p "$STORAGE_ROOT/listings"
mkdir -p "/var/log/restamp"

# 5. Set ownership
echo "[INFO] Setting ownership to $APP_USER:$APP_GROUP..."
chown -R "$APP_USER:$APP_GROUP" "$STORAGE_ROOT"
chown -R "$APP_USER:$APP_GROUP" "/var/log/restamp"

# 6. Set directory permissions:
#    Owner (restamp): rwx (read, write, execute)
#    Group (restamp + www-data): r-x (read, execute to serve files)
#    Others: no access (---)
echo "[INFO] Setting permissions (0750 for directories, 0644 for files)..."
chmod 0750 "$STORAGE_ROOT"
chmod 0750 "$STORAGE_ROOT/listings"
find "$STORAGE_ROOT" -type d -exec chmod 0750 {} +
find "$STORAGE_ROOT" -type f -exec chmod 0644 {} +

# Set group-ID sticky bit so new subdirectories inherit group 'restamp'
chmod g+s "$STORAGE_ROOT"
chmod g+s "$STORAGE_ROOT/listings"

# 7. Check filesystem capacity
AVAILABLE_GB=$(df -BG "$STORAGE_ROOT" | awk 'NR==2 {gsub("G","",$4); print $4}')
echo "[INFO] Storage directory successfully initialized."
echo "[INFO] Available disk space on partition: ${AVAILABLE_GB} GB"

if [[ "$AVAILABLE_GB" -lt 10 ]]; then
    echo "[WARNING] Less than 10 GB disk space available on partition! Monitor disk capacity."
fi

echo "=== Setup Complete ==="
