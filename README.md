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

## Mobile & tablet support

Every page works on phones and tablets, not just desktop-width
screens -- previously none of the pages had a mobile viewport meta
tag at all, which is the main reason things looked broken (tiny,
zoomed-out text) on a phone regardless of any other CSS.

- **Wide data tables** (`/manage`, `/users`, and both tables on
  `/doors`) scroll horizontally within their own container on narrow
  screens rather than squeezing illegibly or breaking the page layout
  -- swipe sideways on a table to see columns that don't fit.
- **Padding and heading sizes shrink** on screens under 600px wide so
  content isn't crowded out by desktop-sized margins.
- The guest-facing welcome screen (`/`) was already built for
  landscape tablets and adapts further down to phone widths too.

## Securing this site

Every page on this site now requires logging in -- previously `/manage`,
`/cleaning`, `/pool`, and `/doors` had no protection at all, despite
being able to control physical pool equipment and send real door
codes.

### First-time setup

On first deploy, no password exists yet. Visiting **any** URL on the
site redirects to `/setup`, which lets you set the password for the
bootstrap `admin` account (username configurable via `ADMIN_USERNAME`,
defaults to `admin`). Once set, `/setup` becomes permanently
unreachable -- it can't be used to create a second account or reset
the password later, by design. From then on, every page requires
logging in at `/login`.

**Set `SECRET_KEY` in Render before your first deploy of this
version.** This signs the login session cookie. Without it, a random
key is generated every time the app starts, which means everyone gets
logged out on every restart or redeploy -- annoying but not a security
hole. Generate one with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
and set it as `SECRET_KEY` in Render's Environment tab.

### The menu page

After logging in with no specific destination in mind (e.g. just
visiting `/login` directly rather than following a link to a specific
page), you land on `/menu` -- a hub linking to every page: the guest
welcome screen, and all the host admin pages. Every admin page's nav
bar also links back to it. Deep links still work as before: if
something redirects you to log in while trying to reach a specific
page (e.g. the lobby tablet loading `/`, or a bookmark straight to
`/pool`), you land on that exact page after logging in, not the menu.

### Managing users

`/users` (linked from every admin page's nav bar) lets you add
additional logins, reset anyone's password, or remove a user. There
are no permission tiers yet -- anyone who can log in can reach every
page, including pool control, sending door codes, and this user list
itself. Keep it to people you'd hand a physical key to. You can't
delete the last remaining user, so you can't accidentally lock
yourself out entirely.

### What's protected against

- **CSRF**: every action (toggling pool equipment, sending a door
  code, adding a user, etc.) requires a per-session token embedded in
  the page that submitted it. A request forged from another site, or
  replayed without a valid token, is rejected.
- **Passwords are hashed**, never stored in plain text.
- **Session cookies** are `HttpOnly` (inaccessible to page JavaScript)
  and `Secure` (only ever sent over HTTPS, which Render provides by
  default).

### What's NOT covered

- No rate limiting on login attempts -- a determined attacker with a
  weak password to guess against isn't slowed down. Use a genuinely
  strong password.
- No password reset via email -- if everyone forgets their password,
  the only recovery path is deleting the `users` table row directly
  via the database (Render Shell) and going through `/setup` again.
- No audit log of who did what.

These are reasonable gaps for a small personal/family-run tool with a
handful of trusted users, but worth knowing about.

## Door codes

`/doors` sends Kwikset keypad access codes to guests -- pick a lock,
pick a month (current + next 5), and for each real guest arriving that
month it shows their name, arrival/departure dates, last 4 digits of
their phone number (their default door code), and their security
deposit status. Press "Send code" to create a code on the selected
lock, valid only from their check-in time to their check-out time.

### Deposit status

Shows "Received ($amount)" or "Not received" per guest, pulled from
OwnerRez's payment records for that booking. OwnerRez has no distinct
payment type for a security deposit -- confirmed against real account
data, deposit payments come back with the same `type` field as regular
payments (`credit_card`), differing only in their description text. So
this detects a deposit by checking whether any payment's description
contains "security deposit" (case-insensitive). If OwnerRez ever
changes how it labels these, this detection would need updating to
match. A failed lookup for one guest shows "Unknown" rather than
breaking the row or the page.

**Note on Airbnb bookings:** Airbnb handles its own damage protection
outside OwnerRez entirely -- checked directly against real account
data (payments, full booking detail, and quotes, across many current
and past bookings) and confirmed there's no deposit record of any kind
for Airbnb-sourced bookings. "Not received" is the factually correct
and expected result for every Airbnb guest; this column is only
meaningful for direct bookings, where OwnerRez itself manages deposit
collection.

### Agreement status

Shows "Signed" or "Not signed" per guest, using OwnerRez's real
`include_agreements` API parameter (confirmed working directly with
OwnerRez support via their developer forum) -- each booking can carry
a list of rental agreements, and an entry with an actual signed date
means it was actually e-signed through OwnerRez's own signing link. A
guest who signed on paper or through another service won't show as
signed here, since OwnerRez itself only records a signature through
its own flow (see OwnerRez's help docs on rental agreements).

### All Door Codes

At the bottom of the page, a second table lists every code this app
has ever sent, across all locks -- guest name, code, slot, the exact
valid window, whether it's expired, and a Remove button. This is
independent of the month/lock filters above, so a stale code from
three months ago still shows up here even if you're currently looking
at a different month.

### One-time setup: connecting Kwikset

This app **does not implement Kwikset login itself** -- Cognito SRP
plus a two-step phone-verification challenge is genuinely complex, and
it's already solved correctly by `auth-setup.js` in the
`kwikset-mcp-node` project. Run that once, on any machine:

```bash
cd kwikset-mcp-node
node auth-setup.js
```

This writes `~/.kwikset-mcp/tokens.json`, which contains an `email`
and a `refreshToken`. Copy both into Render's Environment tab:

```
KWIKSET_EMAIL=you@example.com
KWIKSET_REFRESH_TOKEN=<the refreshToken value from tokens.json>
```

From then on, this app only ever does **token refresh** (simple,
well-documented, no SRP) plus the REST calls -- both far lower-risk
than login itself. If the refresh token is ever revoked or expires
(e.g. you changed your Kwikset password), `/doors` will show a clear
"couldn't refresh" error -- just re-run `auth-setup.js` and update
`KWIKSET_REFRESH_TOKEN`.

### How the code-sending actually works

This talks to Kwikset's real, undocumented cloud API directly (not
through any MCP tool -- those only work inside a Claude chat via a
device bridge, not from a deployed server). The endpoint, headers, and
binary payload format (TLV8 records with packed-BCD digits, not JSON)
were reverse-engineered by decompiling the real Kwikset Android app;
see `kwikset_codec.py` and `kwikset_client.py` for the byte-level
detail and provenance. The encoding was verified three independent
ways during development (an independent reference re-implementation,
hand arithmetic, and a full round-trip decode of a real payload) before
being wired into this app -- but the actual live HTTP calls to
Kwikset's servers couldn't be tested from the sandbox this was built
in. Treat your first real "Send code" as the actual test: **verify the
code works at the keypad or shows up in the Kwikset app afterward.**

### Known limitations

- **"Sent" only means "this app sent it."** Kwikset's API has no way to
  read codes back off the physical lock -- so if a code was added via
  the Kwikset app or the keypad directly, this page has no way to know
  about it, and won't show it as sent.
- **Slot numbers are tracked locally**, starting from slot 50 per lock
  (set via `KWIKSET_START_SLOT`, deliberately leaving 1-49 free since
  those are the slots most likely to already be occupied by codes set
  manually through the Kwikset app), with no visibility into slots
  already used outside this app. If you've added 50+ codes manually via
  the Kwikset app too, check there first to avoid a collision, since
  Kwikset's API doesn't expose a way to check this automatically either.
- **No edit.** To change a sent code, remove it (via the "All Door
  Codes" table at the bottom of the page) and send a new one -- there's
  no in-place edit.
- **"Expired" is a schedule check, not a live lock query.** It's based
  on whether the code's own valid-until time has passed, not a live
  check against the physical lock -- Kwikset's API can't confirm a code
  has actually stopped working, only that its scheduled window has.
- **Removing a code that failed to remove on the lock side stays
  listed.** If the Kwikset API call fails when you click Remove, this
  app deliberately keeps its own record rather than losing track of a
  code that might still be active -- you'll see the real error and can
  retry.
- Guest phone numbers require a separate OwnerRez lookup per guest (not
  included in the booking list) -- if a guest has no phone on file, the
  "Send code" button is disabled for them.

This page has no login — don't share the URL publicly, since it can
create real door access codes.

## Pool control

Visit `/pool` to see live pool/spa readings and control equipment —
pump, heaters, lights, and other switches — via iAqualink (Jandy/
Zodiac). Unlike the other admin pages, this one fetches live on every
page load rather than on an hourly timer, since pump/heater state can
change at any moment and a stale cache would be actively misleading
for a control panel.

To enable it, set two environment variables in Render:
- `IAQUALINK_USERNAME` — the email you use to log into the iAqualink app
- `IAQUALINK_PASSWORD` — that account's password

Leave them unset and `/pool` shows a "not configured" message instead
of erroring.

**Important — Python version requirement:** the `iaqualink` package
this depends on requires Python 3.14+. `render.yaml` already sets
`PYTHON_VERSION=3.14.0` for you; if you're running this locally instead
of on Render, make sure your local Python is 3.14 or newer or pool
control (and only pool control -- everything else still works) won't
import correctly.

**A note on how this was built:** this uses `iaqualink` (the same
unofficial, reverse-engineered library your `iaqualink-mcp` project
wraps) called directly from the Flask app, since a deployed web server
can't reach the MCP tools available inside a Claude chat -- those only
work through your desktop app's device bridge. The categorization
logic (which devices are sensors vs. equipment vs. temperature
controls) was tested against real device data from your actual pool
account, but the live login/API calls themselves couldn't be tested
from the sandbox this was built in (no outbound network access there).
Check `/pool` right after your first deploy with this enabled -- if
anything looks off, share the error message and it's a fast fix.

This page has no login — don't share the URL publicly, since it can
control physical pool equipment.

## Persistent storage (database)

Pool schedules, cleaning checklist progress, and guest selection mode
are saved to a small SQLite database so they survive deploys and
restarts. Without a persistent disk attached, this still works, but
the database file lives on the service's normal (ephemeral) filesystem
and is wiped on every deploy -- effectively back to the old
in-memory-only behavior.

**To make it actually persist, attach a disk:**

1. Render dashboard -> your service -> **Settings** tab -> **Disks** section
2. Click **Add Disk**
3. Name: anything (e.g. `villa-brushy-creek-data`)
4. Mount path: `/var/data`
5. Size: 1 GB is already overkill for this app's data (schedules and
   checklists are a few KB at most) -- costs about $0.25/month
6. Save. Render redeploys automatically once the disk is attached.

Then set this environment variable (Environment tab) so the app
actually writes into that disk instead of the default ephemeral path:
```
DB_PATH=/var/data/app.db
```

`render.yaml` already declares both the disk and `DB_PATH` for
Blueprint-based deploys, but since this service was originally created
as a manual Web Service (not via Blueprint), Render won't auto-apply
either one -- you likely need to add both by hand as described above,
the same situation we ran into earlier with `PYTHON_VERSION`.

**Verifying it's working:** after attaching the disk and setting
`DB_PATH`, add a pool schedule, then trigger a redeploy (any small
`git push`, or Manual Deploy in the dashboard) and check `/pool` again
-- the schedule should still be there. If it's gone after a redeploy,
the disk isn't actually mounted where the app is writing; double-check
the mount path and `DB_PATH` match exactly.

## Pool schedules

At the bottom of `/pool`, set up recurring on/off schedules per piece
of equipment -- e.g. "turn the pool pump on at 8:00 AM, off at 6:00 PM,
every day" or "path lights on 7pm-11pm, weekends only." A background
thread checks every 30 seconds and fires any due schedule, whether or
not anyone has `/pool` open at the time -- that's the whole point of a
schedule.

Notes:
- Times are in the property's local timezone, set via `POOL_TIMEZONE`
  (defaults to `America/Chicago` for Cedar Park, TX). Change this if
  you ever host a property in a different timezone.
- Each schedule fires at most once per calendar day per trigger (on and
  off separately) -- it won't repeatedly toggle a device if the check
  loop happens to run more than once during the matching minute.
- Turning "on" a device that's already on (or "off" one that's already
  off) is a harmless no-op -- schedules use direct on/off commands, not
  toggle, so they're safe to fire even if someone already manually
  changed the equipment's state.
- **Schedules are saved to the database** (see "Persistent storage"
  above) -- they survive deploys and restarts as long as a disk is
  attached and `DB_PATH` points into it. Without that setup, they still
  work but reset on every deploy, same as the rest of this app used to.

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
