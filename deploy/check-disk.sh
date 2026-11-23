#!/usr/bin/env bash
# ==============================================================================
# RESTAMP photo-disk monitor (cron). Exit 0 = ok, 1 = warning, 2 = critical.
#
#   */15 * * * * root /path/to/deploy/check-disk.sh || <ALERT_COMMAND>
#
# Logs to syslog (tag restamp-disk) on warning/critical. Wire the non-zero
# exit to a real alert channel (mail, webhook, uptime monitor) — a log line
# nobody reads is not an alert.
#   MEDIA_ROOT    default /var/www/restamp/uploads
#   WARN_PCT      default 80   (filesystem use %)
#   CRIT_PCT      default 90
# ==============================================================================
set -euo pipefail

MEDIA_ROOT="${MEDIA_ROOT:-/var/www/restamp/uploads}"
WARN_PCT="${WARN_PCT:-80}"
CRIT_PCT="${CRIT_PCT:-90}"

used="$(df -P "$MEDIA_ROOT" | awk 'NR==2 {gsub("%","",$5); print $5}')"
inodes="$(df -Pi "$MEDIA_ROOT" | awk 'NR==2 {gsub("%","",$5); print $5}')"
size="$(du -sh "$MEDIA_ROOT/listings" 2>/dev/null | cut -f1)"
msg="photo disk ${used}% used, inodes ${inodes}%, photos ${size:-?} ($MEDIA_ROOT)"

worst="$used"; [[ "$inodes" =~ ^[0-9]+$ && "$inodes" -gt "$worst" ]] && worst="$inodes"
if (( worst >= CRIT_PCT )); then
    logger -t restamp-disk -p user.crit "CRITICAL: $msg"; echo "CRITICAL: $msg"; exit 2
elif (( worst >= WARN_PCT )); then
    logger -t restamp-disk -p user.warning "WARNING: $msg"; echo "WARNING: $msg"; exit 1
fi
echo "OK: $msg"
