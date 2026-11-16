# ScanMyFood-Backend

Django REST API behind [ScanMyFood](https://scanmyfood.vercel.app). It looks up nutrition data by name, barcode or photo.

## Data sources
- **USDA FoodData Central**: text search and FDC ID lookups (Foundation, SR Legacy, FNDDS and Branded foods).
- **Open Food Facts**: barcode lookups. It adds Nutri-Score, NOVA processing level, allergen and trace tags, additives and product photos. The data is ODbL-licensed, and the frontend shows attribution.
- **Google Gemini**: identifies the food in an uploaded photo. The name it returns is then searched in USDA.

## Endpoints
| Method | Path | Params | Description |
| --- | --- | --- | --- |
| GET | `/health/` | | Redis status, plus the last Gemini outcome and today's Gemini usage (never calls Gemini) |
| GET | `/analyze-many-food/` | `name`, `page` | USDA search |
| GET | `/analyze-food/` | `fcID` | Single USDA food by FDC ID |
| GET | `/barcode/` | `code` (8–14 digits) | Open Food Facts first, then a USDA branded GTIN/UPC match |
| POST | `/analyze-image/` | `image` (multipart), `use_OCR` | Photo → food name (Gemini) → USDA search |

## Environment variables (`api/.env`)
| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `SECRET_KEY` | yes | | Django secret key |
| `DEBUG` | | `False` | `true`/`1` enables debug mode |
| `REDIS_URL` | yes | `redis://localhost:6379/0` | Caching and rate limits |
| `USDA_API_KEY` | yes | | Free from https://fdc.nal.usda.gov/api-key-signup |
| `GEMINI_API_KEY` | for photo scanning | | Free-tier key from https://aistudio.google.com/apikey. Without a billing account it can't incur charges. |
| `GEMINI_MODEL` | | `gemini-3.8-flash` | gemini-2.5-flash is no longer available to new API users |
| `GEMINI_FALLBACK_MODEL` | | `gemini-3.5-flash-lite` | Tried once if the main model is overloaded or unavailable; counts against the same budget |
| `GEMINI_FREE_RPM` | | `5` | Your project's free-tier requests per minute (AI Studio → Rate limits) |
| `GEMINI_FREE_RPD` | | `20` | Your project's free-tier requests per day (resets at midnight Pacific) |
| `GEMINI_QUOTA_SHARE` | | `0.5` | Share of each limit the app may use (0.5 = half) |
| `MAX_IMAGE_REQUESTS_PER_IP` | | `3` | Photo scans that reach Gemini per IP per day (cached photos don't count) |
| `MAX_BARCODE_REQUESTS` | | `120` | Barcode lookups per IP per hour that aren't served from cache |
| `MONTHLY_API_LIMIT` | | `5000000` | Global monthly request cap. When it's exceeded, photo scans are paused. |
| `MAX_IMAGE_BYTES` | | `10485760` | Maximum upload size (10 MB) |
| `TRUSTED_PROXY_COUNT` | | `1` | Number of public proxies that append to `X-Forwarded-For`. It's used to find the real client IP for rate limiting. |

## Running locally
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py runserver
```

## Tests
```bash
SECRET_KEY=test python manage.py test api
```
The tests mock Redis and all external APIs, so they don't need network access or keys.

## Deployment (Oracle VM)
Production runs at `https://scanmyfood.129-80-138-126.sslip.io` as two rootless podman containers (`scanmyfood-api`, `scanmyfood-redis`) managed by systemd user units (`deploy/quadlet/`), behind the VM's nginx.

- **Deploy:** commit, then run `./deploy/deploy.sh`. It uploads `git HEAD`, rebuilds the image, restarts the API and waits for `/health/`.
- **Secrets:** `~/scanmyfood/api.env` on the VM (mode 600), based on `deploy/api.env.example`. Edit it, then restart with `systemctl --user restart scanmyfood-api`.
- **nginx:** `/etc/nginx/conf.d/scanmyfood.conf` comes from `deploy/nginx-scanmyfood.conf`, plus the HTTPS block that certbot added on the server. If you re-copy the repo file, rerun `sudo certbot --nginx -d scanmyfood.129-80-138-126.sslip.io`. Certificates renew automatically through the existing certbot cron job.
- **Logs:** `journalctl --user -u scanmyfood-api -f`
- **Limits:** API memory is capped at 450 MB and Redis at 128 MB. The API runs at lower CPU priority (`Nice=10`) than the VM's other apps. gunicorn handles 8 requests at once (2 workers × 4 threads).

### Under heavy load
| Limit | What happens |
| --- | --- |
| More than 5 requests/s from one IP (burst of 20) | nginx returns `429` JSON right away |
| More than 6 photo uploads/min from one IP (burst of 3) | nginx returns `429` |
| Upload over 10 MB | `413` JSON |
| USDA rate limit (about 1,000 requests/hour per key) | `503` "try again in a minute", and USDA calls pause for 60 s |
| Gemini share used up (daily or per-minute) | `503` "photo scanning isn't available right now" |
| API down or restarting | nginx returns `503` JSON. systemd restarts a crashed API within about 6 s. |
