# RESTAMP Server Storage Deployment & Operations Guide

This guide details the deployment, configuration, security, backup, and rollback procedures for transitioning property-photo uploads from Cloudinary to server disk storage on our Ubuntu production server.

---

## 1. Architecture Overview

- **Storage Engine**: Server disk directory `/var/lib/restamp/property-photos/listings/`
- **Static Serving**: Directly via Nginx at `https://api.restamp.in/media/<listing_id>/<uuid>.<ext>`
- **Application Engine**: FastAPI running under Uvicorn managed by `systemd`
- **Reverse Proxy**: Nginx forwarding API requests (`/`) and directly serving static media (`/media/`)
- **Backward Compatibility**:
  - Legacy Cloudinary images retain their full URLs (`https://res.cloudinary.com/...`).
  - Database `property_media` rows store either `local:<listing_id>/<uuid>.<ext>` or Cloudinary public IDs.
  - Deletions are dispatched automatically to the appropriate backend.
  - Profile photos and non-property categories continue using Cloudinary.

---

## 2. Directory Structure & Permissions

### Storage Hierarchy
```
/var/lib/restamp/property-photos/
└── listings/
    ├── 101/
    │   ├── 4a8e2b9c7d1f40a1b2c3d4e5f6a7b8c9.jpg
    │   └── e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6.webp
    └── 102/
        └── 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c.png
```

### Permissions Setup
Run the initialization script as root:
```bash
sudo chmod +x /var/www/restamp/backend/deploy/setup-storage.sh
sudo /var/www/restamp/backend/deploy/setup-storage.sh
```

**Permission Specifications**:
- `restamp:restamp` owns `/var/lib/restamp/property-photos`
- `www-data` is added to the `restamp` group
- Directories: `0750` (`rwxr-x---`)
- Files: `0644` (`rw-r--r--`)
- Directory sticky bit `g+s` ensures newly created listing folders inherit group `restamp`

---

## 3. Nginx Configuration

1. Copy the configuration file:
   ```bash
   sudo cp deploy/nginx/restamp-media.conf /etc/nginx/sites-available/restamp.conf
   sudo ln -sf /etc/nginx/sites-available/restamp.conf /etc/nginx/sites-enabled/restamp.conf
   ```
2. Test Nginx syntax:
   ```bash
   sudo nginx -t
   ```
3. Reload Nginx:
   ```bash
   sudo systemctl reload nginx
   ```

**Key Nginx Directives**:
- `alias /var/lib/restamp/property-photos/listings/;` maps `/media/` directly to storage.
- Regex location enforces `^/media/([0-9]+)/([a-f0-9]{32})\.(jpg|jpeg|png|webp)$` preventing directory traversal or execution of unauthorized extensions.
- `add_header X-Content-Type-Options "nosniff";` prevents MIME confusion attacks.
- `expires 30d; add_header Cache-Control "public, no-transform, immutable";` leverages UUID immutability for optimal caching.

---

## 4. Systemd Service Configuration

1. Install systemd unit:
   ```bash
   sudo cp deploy/systemd/restamp-backend.service /etc/systemd/system/restamp-backend.service
   sudo systemctl daemon-reload
   ```
2. Configure production secrets in `/etc/restamp/backend.env` (permissions `0600`):
   ```bash
   sudo mkdir -p /etc/restamp
   sudo cp deploy/env.production.example /etc/restamp/backend.env
   sudo chmod 0600 /etc/restamp/backend.env
   sudo chown root:restamp /etc/restamp/backend.env
   ```
3. Start and enable service:
   ```bash
   sudo systemctl enable restamp-backend
   sudo systemctl restart restamp-backend
   ```

---

## 5. Environment Variables Reference

| Variable | Recommended Production Value | Description |
|---|---|---|
| `RESTAMP_PHOTO_STORAGE` | `local` | Directs NEW property-photo uploads to server disk. |
| `RESTAMP_MEDIA_ROOT` | `/var/lib/restamp/property-photos` | Root directory path on Ubuntu disk. |
| `RESTAMP_MEDIA_PUBLIC_BASE_URL` | `https://api.restamp.in` | Base URL used to build permanent media URLs. |
| `RESTAMP_PHOTO_MAX_BYTES` | `10485760` (10 MB) | Maximum allowed upload size (bytes). Range: 64 KB to 50 MB. |
| `RESTAMP_PHOTO_MAX_DIMENSION` | `10000` | Maximum pixel width/height. Range: 256 to 30,000 px. |
| `RESTAMP_TRUST_PROXY` | `1` | Enables secure client IP extraction behind Nginx. |
| `CLOUDINARY_CLOUD_NAME` | *(Retained)* | Required for legacy photo deletion & profile photos. |

---

## 6. Backup and Restore Procedures

The script `deploy/backup-restore.sh` automates atomic snapshots and restoration.

### Manual Backup:
```bash
sudo chmod +x deploy/backup-restore.sh
sudo deploy/backup-restore.sh backup
```
- Creates a timestamped `.tar.gz` under `/var/backups/restamp/photos/`.
- Generates a `.sha256` checksum for integrity verification.
- Retains rolling 14-day history.

### Verification:
```bash
sudo deploy/backup-restore.sh verify /var/backups/restamp/photos/property_photos_YYYYMMDD_HHMMSS.tar.gz
```

### Restore:
```bash
sudo deploy/backup-restore.sh restore /var/backups/restamp/photos/property_photos_YYYYMMDD_HHMMSS.tar.gz
```

---

## 7. Zero-Downtime Rollback Procedure

If local storage encounters filesystem, disk capacity, or server issues, rollback to Cloudinary is instantaneous and requires **zero database schema changes or data loss**:

1. Edit `/etc/restamp/backend.env`:
   ```ini
   RESTAMP_PHOTO_STORAGE=cloudinary
   ```
2. Restart the FastAPI service:
   ```bash
   sudo systemctl restart restamp-backend
   ```
3. **Outcome**:
   - All NEW uploads immediately route to Cloudinary.
   - All photos uploaded to the server during the migration continue to be served via Nginx without disruption.
   - Mixed galleries remain fully supported.
