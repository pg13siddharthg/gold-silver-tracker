# Gold & Silver · India — personal price tracker

A free, no-server setup that:

* **checks gold and silver prices once a day** (GitHub Actions, free),
* **sends an Android notification when prices fall** (ntfy app, free),
* gives you a **home-screen app** with live prices, a trend chart and an alert log (GitHub Pages, free).

```
GitHub Actions (daily, 12:00 IST)
   └─ scripts/check_prices.py ── fetch XAU/XAG in INR ──► docs/history.json
                │                                          │
                └─ fall detected? ──► ntfy.sh ──► 📱 notification
                                                           ▼
                                   Phone app (docs/ on GitHub Pages) reads history + live price
```

## What counts as a "fall" (edit `docs/config.json`)

| Rule | Gold (24K /10g) | Silver (/kg) |
|---|---|---|
| Drop vs previous day | 1% | 2% |
| Drop from 30-day high (sent once, when first crossed) | 3% | 5% |
| Below a price you set (`below_price`, e.g. `125000`) | off | off |

With `daily_summary: true` you also get a quiet daily price note on days with no alert. Set it to `false` to hear only about falls.

**Indian price** = international spot (converted to INR) × **15% import duty**, the rate in effect since 13 May 2026. GST (3%) is off by default; set `"include_gst": true` to add it. If the duty changes, update `import_duty_pct`. Jewellers add making charges and local premiums, but % moves, and so the alerts, match closely.

---

## Setup (about 15 minutes, one time)

### 1. Install ntfy on your phone
Install **ntfy** from the Play Store → **+** → subscribe to a topic with a hard-to-guess name, e.g. `sid-gold-7f3k92q`. Anyone who knows the name can read it, so treat it like a password.

### 2. Create the GitHub repo
1. Create a free GitHub account if needed, then **New repository** → name it e.g. `gold-silver-tracker` → **Public** (free GitHub Pages needs public; the repo only holds prices, never your topic).
2. **Add file → Upload files** → drag in the *contents* of this folder, including the `.github` folder. (If the web uploader skips the hidden `.github` folder, create `.github/workflows/daily-check.yml` with **Add file → Create new file** and paste in its contents.)

### 3. Add your ntfy topic as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**
Name: `NTFY_TOPIC`  Value: your topic name (e.g. `sid-gold-7f3k92q`)

### 4. Allow the job to save history
Repo → **Settings → Actions → General → Workflow permissions** → **Read and write permissions** → Save.

### 5. Turn on the phone app (GitHub Pages)
Repo → **Settings → Pages** → Source: *Deploy from a branch* → Branch `main`, folder **`/docs`** → Save.
After a minute your app is at `https://<your-username>.github.io/gold-silver-tracker/`.
Optional: put that URL into `dashboard_url` in `docs/config.json`, so tapping a notification opens the app.

### 6. Test it
Repo → **Actions → Daily gold & silver check → Run workflow**. Within about a minute you should get a notification with today's prices. After that it runs every day by itself.

### 7. Install the app on your phone
Open the Pages URL in Chrome → tap **Install as app** (or ⋮ → *Add to Home screen*).

---

## Notes
* **Data source:** the free, key-less [fawazahmed0 currency API](https://github.com/fawazahmed0/exchange-api) (daily XAU/XAG rates in INR) with a backup mirror. If both are down, you get a low-priority "check failed" notification.
* **Timing:** change the `cron` line in `.github/workflows/daily-check.yml` (UTC; `30 6 * * *` = 12:00 IST). GitHub can delay scheduled runs by a few minutes.
* **Charts fill in over time:** history starts from your first run.
* **Test locally:** `python scripts/check_prices.py --dry-run` prints what it would send.
