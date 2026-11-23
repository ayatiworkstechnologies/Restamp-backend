# RESTAMP property-photo server storage: deployment runbook

New **property photos** can be stored on the Ubuntu server's disk and served by Nginx. Existing Cloudinary photos stay on Cloudinary, keep displaying, and are still deleted through Cloudinary. Nothing is migrated, and Cloudinary must stay configured.

Values in `<ANGLE_BRACKETS>` are placeholders. Verify each one on the server; none of them are known facts.

| Environment | `RESTAMP_PHOTO_STORAGE` | Why |
|---|---|---|
| Ubuntu production (persistent disk) | `local` | Photos under `/var/www/restamp/uploads` |
| Render (no persistent disk) | `cloudinary` (default) | Render's filesystem is wiped on every deploy/restart |
| Local development | either | `local` writes to `/tmp/restamp-media` by default |

## 1. How it works

- **Upload:** `POST /owner/listings/{id}/photos` validates the photo:
  - the owner, the paid plan and the 20-photo cap
  - MIME type + extension + magic bytes
  - size (`RESTAMP_PHOTO_MAX_BYTES`, default 10 MiB) and header dimensions (`RESTAMP_PHOTO_MAX_DIMENSION`, default 10000 px)

  It then strips EXIF/GPS, XMP, IPTC and text metadata, keeping only the orientation tag, and writes the file atomically to `<RESTAMP_MEDIA_ROOT>/listings/<listing_id>/<uuid32>.<jpg|png|webp>`.
- **Database:** `property_media.url` = `<RESTAMP_MEDIA_PUBLIC_BASE_URL>/media/<listing_id>/<uuid>.<ext>` (absolute https). `property_media.public_id` = `local:<listing_id>/<uuid>.<ext>`. Cloudinary rows keep their own URL and public id. There's no schema change, and API fields (`cover_image_url`, `gallery`, `photos`, `PhotoUploadOut.url`) are unchanged.
- **Delete:** single delete, or PATCH removing a photo, routes by `public_id`: `local:` deletes the file on disk, anything else goes through Cloudinary destroy. Soft-deleting a listing keeps its files.
- **Startup refuses**, in production:
  - `local` without an https `RESTAMP_MEDIA_PUBLIC_BASE_URL`
  - a `RESTAMP_MEDIA_ROOT` that is relative, under `/tmp`, `/var/tmp`, `/dev/shm`, `/run`, the system temp dir, the filesystem root, or inside the repository
  - malformed photo limits
  - `local` on Render without `RESTAMP_RENDER_PERSISTENT_DISK=1`

## 2. Inspect before changing anything

Read-only, and never print env files or secrets:

```bash
systemctl list-units --type=service | grep -i restamp       # -> <UNIT>
systemctl cat <UNIT>                                         # ExecStart, User, EnvironmentFile, WorkingDirectory
systemctl show -p User,Group,MainPID <UNIT>
sudo ss -ltnp | grep -E ':8000|uvicorn'
sudo nginx -T 2>/dev/null | grep -nE 'server_name|location|alias|client_max_body_size|proxy_pass'
sudo grep -cE '^RESTAMP_(PHOTO_STORAGE|MEDIA_ROOT|MEDIA_PUBLIC_BASE_URL)=' <ENV_FILE>   # counts only
df -h /var/www
```

## 3. Storage directory

```bash
sudo deploy/setup-storage.sh <SERVICE_USER> <SERVICE_GROUP> /var/www/restamp/uploads
```

This creates `/var/www/restamp/uploads` and `/var/www/restamp/uploads/listings`, owned by the service user with mode `0755`.
- **Permissions:** only the backend can write, and Nginx can read. The photos are public, so world-read is intended. Never use `chmod 777`.
- **Safety:** the script creates no users, never deletes files, and proves the service user can write.
- **Location:** the directory is outside the git checkout, so deploys and `git clean` can't touch it.

## 4. Backend service

1. Back up the env file (`sudo cp -p <ENV_FILE> <ENV_FILE>.bak-$(date +%F)`), then set:
   `RESTAMP_PHOTO_STORAGE=local`, `RESTAMP_MEDIA_ROOT=/var/www/restamp/uploads`, `RESTAMP_MEDIA_PUBLIC_BASE_URL=https://<API_HOST>`.
   Keep all Cloudinary variables, and don't touch any OTP setting. See `env.production.example`.
2. Compare the existing unit with `systemd/restamp-backend.service.template`. The required properties:
   - **One Uvicorn worker** (`--workers 1`, and no `WEB_CONCURRENCY` > 1). The background loops (auto-verify, email, push, reminders, rate-limit cleanup) run in every worker process, so more workers would duplicate sends. The backend logs a warning at startup if `WEB_CONCURRENCY` > 1.
   - **No `--loop uvloop` / `--http httptools`:** these aren't in `requirements.txt`, and the service would fail to start.
   - **`--proxy-headers --forwarded-allow-ips 127.0.0.1`** behind the local Nginx, with `RESTAMP_TRUST_PROXY=1`.
   - **If the unit uses `ProtectSystem=strict`:** `ReadWritePaths=/var/www/restamp/uploads`.
   - **Database:** Aiven MySQL is remote and needs TLS. `RESTAMP_DB_SSL_CA` must point at a CA file the service user can read. There's no local `mysql.service` dependency.
3. Apply with `sudo systemd-analyze verify <unit file>`, then `sudo systemctl daemon-reload`, then `sudo systemctl restart <UNIT>`. Check `systemctl status <UNIT>` and `journalctl -u <UNIT> -n 50`. A startup refusal names the setting, never the value.

## 5. Nginx

1. Back up: `sudo cp -p /etc/nginx/sites-available/<VHOST> /root/<VHOST>.bak-$(date +%F)`.
2. Install the snippet: `sudo install -m 0644 deploy/nginx/restamp-media.location.conf /etc/nginx/snippets/`.
3. Inside the existing HTTPS `server {}` for `<API_HOST>`, add `include /etc/nginx/snippets/restamp-media.location.conf;` and make sure `client_max_body_size` is at least `RESTAMP_PHOTO_MAX_BYTES` + 1 MiB. For the default 10 MiB, use `client_max_body_size 12m;`. If a different `/media/` location already exists, remove it in the same edit.
4. Run `sudo nginx -t`. **Reload only if it succeeds** (`sudo systemctl reload nginx`). If it fails, restore the backup.

What the snippet provides:
- **Access:** GET/HEAD only; exact `^/media/<id>/<32 hex>.(jpg|png|webp)$` names only, and everything else under `/media/` returns 404 (no traversal, no directory listing, no other files); a missing file returns 404.
- **Headers:** fixed MIME types; one `Cache-Control: public, max-age=2592000, immutable`; `nosniff` and the other security headers repeated inside the block, because `add_header` doesn't inherit.

## 6. Backups: required before calling storage production-ready

The photos exist only on this disk, so a **local archive is not a backup**.

```bash
# daily, as root (cron or a systemd timer):
a=$(MEDIA_ROOT=/var/www/restamp/uploads deploy/backup-restore.sh backup | tail -1) \
  && OFFSITE_TARGET='<BACKUP_USER>@<OTHER_HOST>:/srv/restamp-photos/' deploy/backup-restore.sh offsite "$a"
```

- **Off-site:** `OFFSITE_TARGET` must be on another machine or provider. `rclone copy "$a"* <REMOTE>:<BUCKET>/` to S3/B2/R2 is equivalent. Alert on a non-zero exit.
- **Database coordination:** photo rows live in Aiven MySQL, which has its own managed backups.
  - Run the photo backup **after** the Aiven backup window, so that every `local:` row in a DB backup has its file in a same-day or newer photo archive.
  - Restore the database first, then the photos.
  - Files without rows are harmless orphans. A row without a file shows a broken image.
- **Restore:** `SERVICE_USER=<u> SERVICE_GROUP=<g> deploy/backup-restore.sh restore <archive>`. It verifies the checksum, extracts to staging, and copies **only missing** files, so nothing is overwritten or deleted.
- **Verify a restore:**
  1. Compare the archive's `.manifest` with `SELECT public_id FROM property_media WHERE public_id LIKE 'local:%'`. Every `local:<path>` should exist as `listings/<path>`.
  2. Then `curl -sI https://<API_HOST>/media/<listing_id>/<uuid>.jpg` should return `200` with `Content-Type: image/jpeg`.
- **Testing:** run a restore into a scratch `MEDIA_ROOT` at least once, and after any change to the procedure.

## 7. Disk monitoring

```cron
*/15 * * * * root MEDIA_ROOT=/var/www/restamp/uploads /path/to/deploy/check-disk.sh || <ALERT_COMMAND>
```

`check-disk.sh` warns at 80% and goes critical at 90% (block or inode use). Wire `<ALERT_COMMAND>` to mail, a webhook or an uptime monitor.

For capacity, each listing holds at most 20 × `RESTAMP_PHOTO_MAX_BYTES`, and only paid posters can upload.

## 8. Verification (controlled test listing only)

Use a dedicated test owner and listing, never a customer's.
1. Upload a photo. The response `url` is `https://<API_HOST>/media/<id>/<uuid>.<ext>`.
2. The file exists under `/var/www/restamp/uploads/listings/<id>/`.
3. The row has `public_id = local:<id>/<uuid>.<ext>`.
4. `curl -sI` on the URL returns 200 with the correct type and the cache/nosniff headers.
5. The image appears in `GET /owner/listings/{id}` (`photos`, `cover_image_url`).
6. Restart the service. The file and URL still work.
7. Delete the photo. The file is gone and the URL returns 404.
8. A listing with Cloudinary photos still displays them.

## 9. Rollback

1. Set `RESTAMP_PHOTO_STORAGE=cloudinary` in the env file and restart the unit. New uploads go to Cloudinary again.
2. **Keep** the Nginx snippet and `/var/www/restamp/uploads`. Photos already uploaded to disk are referenced by their rows and must keep being served. Their deletes still work, because routing goes by `public_id`.
3. Never delete the storage directory while any `local:` row exists.

## 10. Render

`render.yaml` doesn't set `RESTAMP_PHOTO_STORAGE`, so Render uses Cloudinary.
- **Don't set `local` on Render** unless a Render persistent disk is mounted and verified at `RESTAMP_MEDIA_ROOT`.
- **Even then**, startup refuses `local` on Render until `RESTAMP_RENDER_PERSISTENT_DISK=1` is set.
