# Villa Brushy Creek — Welcome Screen

A live web page that always shows the next arriving guest, pulled from
OwnerRez and refreshed automatically every hour. Deployed on Render.

## Files

- `app.py` — the Flask app (background thread refreshes OwnerRez hourly)
- `requirements.txt` — Python dependencies Render installs
- `render.yaml` — Render deployment blueprint (optional but recommended)

## 1. Push this folder to GitHub

From inside this folder:

```bash
git init
git add .
git commit -m "Initial commit: welcome screen app"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo-name>.git
git push -u origin main
```

If you don't have a repo yet: go to github.com -> New repository -> give
it a name (e.g. `villa-brushy-creek-welcome`) -> Create repository
(leave it empty, no README/gitignore) -> then run the commands above.

## 2. Deploy on Render

**Option A — using render.yaml (recommended):**
1. Render dashboard -> New -> Blueprint
2. Connect the GitHub repo you just pushed
3. Render reads `render.yaml` automatically and proposes the service
4. Before deploying, add your two environment variables when prompted:
   - `OWNERREZ_USERNAME`
   - `OWNERREZ_TOKEN`
5. Deploy

**Option B — manual setup:**
1. Render dashboard -> New -> Web Service
2. Connect the GitHub repo
3. Runtime: Python 3
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app --workers 1 --threads 4 --timeout 60`
6. Instance type: **Starter** ($7/mo, always-on) or **Free** (sleeps
   after 15 min idle — fine for testing, not for the tablet long-term)
7. Environment tab -> add `OWNERREZ_USERNAME` and `OWNERREZ_TOKEN`
8. Optional -- add Wi-Fi auto-join QR code: also set `WIFI_SSID` and
   `WIFI_PASSWORD` (see "Wi-Fi QR code" section below). Skip these two
   to leave the Wi-Fi section off the page entirely.
9. Create Web Service

Render will give you a URL like:
```
https://villa-brushy-creek-welcome.onrender.com
```
Point the tablet's browser at that URL. It updates itself hourly — no
further action needed on your end.

## 3. Updating later

Any time you edit `app.py` (e.g. to add Wi-Fi info to the template),
just commit and push again:

```bash
git add .
git commit -m "Update welcome screen"
git push
```

Render auto-redeploys on every push to `main`.

## Cleaning checklist

Visit `/cleaning` to see a per-guest cleaning checklist for the next 5
upcoming bookings. Check off tasks as they're done — each checkbox
saves instantly (no submit button needed). A progress bar and "Ready"
badge show at a glance which turnovers are complete.

The default checklist covers the usual turnover tasks (strip/remake
beds, clean bathrooms and kitchen, vacuum, trash, restock supplies,
pool/spa check, exterior walk, final walkthrough). To customize it,
set the `CLEANING_TASKS` environment variable to your own
comma-separated list, e.g.:

```
CLEANING_TASKS=Strip beds,Clean bathrooms,Vacuum floors,Check hot tub,Restock coffee
```

Checklists are tracked per booking and automatically reset (cleared
from memory) once that booking is no longer upcoming -- so each new
turnover starts fresh with nothing checked. Progress is stored in
memory only, so a Render restart or redeploy clears any in-progress
checklists.

This page also has no login — don't share the URL publicly.

## Managing which guest is shown

Visit `/manage` on your deployed URL (e.g.
`https://villa-brushy-creek-welcome.onrender.com/manage`) to see the
next 5 upcoming bookings and control which one the welcome screen
shows:

- **Auto** (default) — always shows whoever's arriving soonest.
- **Manual** — pick any of the next 5 guests to pin on the welcome
  screen, e.g. if you want to show tomorrow's arrival a day early even
  though someone else technically arrives sooner. It stays pinned
  through the hourly refresh until you pick someone else or switch
  back to Auto. If the pinned guest's booking disappears from the
  upcoming list (checked out, or the reservation was cancelled), it
  automatically falls back to Auto rather than showing stale data.

This page has no login — anyone with the URL can change what's shown,
so don't post `/manage` publicly the way you might the main `/` URL.

## Wi-Fi QR code

To show a scannable "join Wi-Fi automatically" QR code on the welcome
screen, set these environment variables in Render (Environment tab):

- `WIFI_SSID` -- your guest network name
- `WIFI_PASSWORD` -- the network password
- `WIFI_AUTH` -- optional, defaults to `WPA` (covers WPA/WPA2/WPA3, i.e.
  virtually every home router). Use `WEP` for an old WEP network, or
  `nopass` for an open network with no password.

If `WIFI_SSID` isn't set, the Wi-Fi section simply doesn't appear on the
page -- nothing breaks either way.

The QR code is generated once when the app starts (not regenerated on
every hourly refresh, since the network doesn't change), and is a
standard Wi-Fi QR payload that iOS and Android both recognize natively
through the camera app -- no extra app needed to scan and join.

## Notes

- `--workers 1` in the start command is deliberate. This app refreshes
  OwnerRez itself on a background thread once an hour. Running more
  than one gunicorn worker would spin up that same background thread
  multiple times, multiplying API calls for no benefit.
- Never commit real OwnerRez credentials into this repo. They're read
  from environment variables (`OWNERREZ_USERNAME`, `OWNERREZ_TOKEN`),
  which you set in Render's dashboard, not in code.
- `/status` (e.g. `https://your-app.onrender.com/status`) shows the
  last successful refresh time and any error, useful for a quick check
  that OwnerRez is being reached correctly.
- `/refresh` forces an immediate refresh instead of waiting up to an
  hour — handy right after you've made a booking change.
