# Facebook Page Comment Replier (localhost)

A small website you run on your own computer. It logs into Facebook with
**your** account, reads comments on **your Page's** post/reel, and replies to
each recent comment with the text you paste — one by one, with a safe delay
between replies. Replies are posted **as your Page**.

Works for Facebook **Pages** only. It cannot reply on personal-profile posts
(Facebook's official API simply doesn't allow that for anyone).

---

## Step 1 — Create the Meta app (one time, ~10 min)

1. Go to **developers.facebook.com** and log in with your Facebook account.
2. **My Apps → Create App**. Choose type **Business** (or "Other" → Business).
3. App name: anything, e.g. `Page Comment Replier`. Click **Create app**.
4. In the left sidebar, **Add Product** → **Facebook Login** → **Set up**.
   (If it asks for a use case, pick the business/page one.)
5. Go to **Facebook Login → Settings** (left sidebar under the product).
6. Under **Valid OAuth Redirect URIs**, add exactly:
   `http://localhost:5000/api/callback`
   Click **Save Changes**.
7. Go to **Settings → Basic** (left sidebar). Copy your **App ID** and
   **App Secret** (click Show).
8. Keep the app in **Development mode** (don't switch to Live).
   In Development mode it works for you (the admin) with no app review needed.

> Only you can use this app — that's fine, it's your tool on your computer.

## Step 2 — Install & run

You need **Python 3.9+** installed. Then in a terminal, inside this folder:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config file
cp .env.example .env        # Windows: copy .env.example .env

# 3. Edit .env and paste your FB_APP_ID and FB_APP_SECRET
#    (the app loads this file automatically — no extra command needed)

# 4. Start the server:
python app.py
```

Open **http://localhost:5000** in your browser.

## Step 3 — Use it

1. **Login with Facebook** — use the account that manages your Page.
   Grant the requested permissions (`pages_read_engagement`,
   `pages_manage_posts`, `pages_manage_engagement`).
2. **Choose your Page** from the dropdown.
3. **Paste the post/reel link.** Open the reel in your browser and copy the
   address-bar URL — it looks like `facebook.com/reel/123456789...`.
   (Share links like `/share/r/…` don't work here; the address-bar URL does.)
   Set how many hours back to look (default 48) → **Fetch comments**.
4. **Review the list**, uncheck any you want to skip → **Confirm selection**.
5. **Paste your reply text**, set the delay (30 seconds recommended),
   → **Start sending**. You can **Stop** anytime; already-sent comments are
   remembered in `sent_replies.json` and skipped on the next run.

### ⚡ Watched Posts (auto mode — many posts, hands-free)

Want every *new* comment on one or **many** posts to get your reply
automatically, without fetching/reviewing each time?

1. Log in and choose your **Page** in Step 2.
2. Scroll to the **⚡ Watched Posts** card at the bottom.
3. Paste a **post/reel link**, check/edit the **reply text** (your saved
   text is prefilled), set the gap between replies (default 60 seconds),
   and press **➕ Add to Watch List**. Repeat for as many posts as you
   like — each post can have its own reply text.
4. A background watcher checks **all** watched posts every 2 minutes
   (change with `WATCH_INTERVAL_SEC`) and replies to new comments
   automatically. The list shows each post's status, total replies sent,
   the last check time and any error. **Pause/Resume** or **Remove** any
   post anytime; pausing never backfills old comments when you resume.

Good to know about auto mode:

- Only comments that arrive **after** a post is added to the list get a
  reply. Older comments are handled by the manual flow above.
- Only **top-level comments** are watched (replies to other comments are
  not) — this matches what Facebook's API exposes simply and safely.
- Your login tokens are saved **on the server** (in `fb_store.json`, next
  to the app), so the watcher keeps working after you close the browser
  tab. Logging out in the app deletes the saved tokens and stops
  watching. The long-lived token lasts ~60 days — when replies stop with
  a token error, just log in again.
- The watch list itself is saved in `watches.json`, so your posts stay
  watched across app restarts.
- **The app process must stay running** for auto replies to flow. On your
  laptop that means the `python app.py` window stays open and the laptop
  stays on. For replies while your laptop is OFF, host the app on an
  always-on server — see the next section.

## Run it 24×7 on a server (optional — replies while your laptop is off)

Any always-on host that runs Python works (Render, Railway, a VPS, …).
The repo includes a `Procfile` for the common platforms:

```
web: gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT app:app
```

> ⚠️ **Keep exactly ONE worker (`-w 1`).** The watcher is a background
> thread inside the app; with more workers each one would start its own
> watcher and posts would get checked (and replied) multiple times.

On the server, set these environment variables (same values as `.env`):

- `FB_APP_ID`, `FB_APP_SECRET`, `FLASK_SECRET` (a long random string —
  keep it fixed so logins survive restarts)
- `DASHBOARD_PASSWORD` — **always set this on a public server.** Anyone
  who opens your app's URL will first get a small password form; without
  it, anyone could use your logged-in Page tokens.
- `FB_REDIRECT_URI` — your server's callback URL, e.g.
  `https://your-app.onrender.com/api/callback`, and add that **exact**
  URL under Facebook Login → Settings → Valid OAuth Redirect URIs in
  your Meta app.

Then log in once from the hosted dashboard, add your posts to the watch
list, and you're done — the server keeps replying day and night.

## Good to know

- **Replies come from your Page**, not your personal profile.
- **Go slow.** Sending the identical message to hundreds of comments too fast
  can trigger Facebook's spam limits and restrict the Page. The default
  30-second gap is there for safety — don't set it to 5s for a big batch.
- **Tokens:** the login gives a long-lived token (~60 days). The user
  token and Page tokens are saved server-side in `fb_store.json` so the
  watcher survives browser closes and app restarts. If the app one day
  says "not logged in", just log in again. Logging out deletes the saved
  tokens. Treat `fb_store.json` like a password — never share it.
- **Dashboard password:** set `DASHBOARD_PASSWORD` in `.env` and the
  whole dashboard (including the API) sits behind a password form at
  `/gate`. Leave it empty for laptop/localhost use only.
- **Already-replied comments are skipped** automatically, even across restarts
  (`sent_replies.json` in this folder — don't delete it mid-campaign).
- **Extra duplicate protection in auto mode:** before replying, the watcher
  also asks Facebook whether the Page has already replied to that comment.
  So even if the local state files are wiped (free hosts like Render have a
  temporary disk), a comment can never receive the same reply twice.
- **One account at a time:** to use a different Facebook account/Page, click
  Logout in the app and log in again with the other account.
- Keep your **App Secret** private. Never share the `.env` file.

## Troubleshooting

| Problem | Fix |
|---|---|
| "FB_APP_ID / FB_APP_SECRET not set" | You skipped Step 2 — create `.env` and restart. |
| "Invalid OAuth redirect URI" / redirect error on login | The URI in Facebook Login → Settings must match `FB_REDIRECT_URI` **exactly**, including `http://` and the port. |
| "Can't read that post" | The post must belong to the Page you selected. Check the link. |
| No Pages in the dropdown | The login account must have a role on (manage) at least one Facebook Page. |
| Replies fail with permission error | Log out, log in again and accept all permissions. |
| Watched post shows "Page token missing" | Tokens were cleared (logout) or expired. Log in again and open the dashboard once so tokens re-save to `fb_store.json`. |
| Auto replies stopped while laptop was off | Expected on a laptop — the app must be running. Host it on an always-on server (see "Run it 24×7") for round-the-clock replies. |
