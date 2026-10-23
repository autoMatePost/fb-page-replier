#!/usr/bin/env python3
"""
Facebook Page Comment Replier — localhost web app.

Flow:
  1. Login with Facebook (OAuth) -> grants pages_read_engagement + pages_manage_posts
  2. Pick one of your Pages
  3. Paste the post/reel link (or post ID) -> resolves to a post ID
  4. Fetch all comments -> keeps only the last N hours -> you review & confirm
  5. Paste the reply text -> confirm -> replies are sent one by one (as the Page)

Auto mode: add posts to the Watched Posts list and a background manager
replies to every new comment on them. Tokens are kept server-side
(fb_store.json) and the watch list is persisted (watches.json), so the
watcher keeps running even after the browser tab/session is gone — run
the app on an always-on server and replies continue while your laptop
is off. See README.md.

Run locally:  python app.py   then open http://localhost:5000
"""

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, render_template, request, session

# ---------------------------------------------------------------- config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "sent_replies.json")  # post_id -> [comment ids already replied]
STATE_LOCK = threading.Lock()  # guards sent_replies.json between UI sends and the watcher thread
STORE_FILE = os.path.join(BASE_DIR, "fb_store.json")  # server-side tokens: user token + page tokens
STORE_LOCK = threading.Lock()  # guards fb_store.json
WATCHES_FILE = os.path.join(BASE_DIR, "watches.json")  # persisted multi-post watch list
WATCH_LOCK = threading.Lock()  # guards watches.json


def load_dotenv(path):
    """Load KEY=VALUE lines from .env into the environment (no dependency)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


load_dotenv(os.path.join(BASE_DIR, ".env"))

APP_ID = os.environ.get("FB_APP_ID", "").strip()
APP_SECRET = os.environ.get("FB_APP_SECRET", "").strip()
REDIRECT_URI = os.environ.get(
    "FB_REDIRECT_URI", "http://localhost:5000/api/callback"
).strip()
# If set, the whole dashboard sits behind a simple password form (/gate).
# Leave unset on your own laptop — set it when hosting on a public server.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
try:
    POLL_INTERVAL = max(30, int(os.environ.get("WATCH_INTERVAL_SEC", "120")))
except ValueError:
    POLL_INTERVAL = 120

GRAPH = "https://graph.facebook.com/v22.0"
SCOPES = "pages_read_engagement,pages_manage_posts,pages_manage_engagement"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(24))


# ---------------------------------------------------------------- helpers

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_store():
    """Server-side credential store: {user_token, pages: {id: {name,
    access_token}}, selected_page_id}. Lets the watcher run without any
    browser session."""
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_store(store):
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def clear_store():
    with STORE_LOCK:
        try:
            if os.path.exists(STORE_FILE):
                os.remove(STORE_FILE)
        except OSError:
            save_store({})


def load_watches():
    if os.path.exists(WATCHES_FILE):
        try:
            with open(WATCHES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_watches(watches):
    with open(WATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(watches, f, indent=2)


def graph_get(path, params=None):
    r = requests.get(f"{GRAPH}{path}", params=params or {}, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "Graph API error"))
    return data


def graph_post(path, params=None):
    r = requests.post(f"{GRAPH}{path}", data=params or {}, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "Graph API error"))
    return data


def page_token():
    tok = session.get("page_token")
    if not tok:
        raise RuntimeError("Page not selected. Login again.")
    return tok


def _restore_session_from_store():
    """After a server restart the browser session is empty but the store
    still has the tokens — refill the session so the manual flow (and the
    Pages dropdown) keeps working without a fresh Facebook login."""
    if session.get("user_token"):
        return
    store = load_store()
    tok = store.get("user_token")
    if not tok:
        return
    session["user_token"] = tok
    pages = store.get("pages") or {}
    session["page_tokens"] = {
        pid: p.get("access_token")
        for pid, p in pages.items() if p.get("access_token")
    }
    sel = store.get("selected_page_id")
    if sel and sel in session["page_tokens"]:
        session["page_id"] = sel
        session["page_token"] = session["page_tokens"][sel]


def resolve_post_id(raw):
    """Turn a pasted link or ID into a numeric post ID."""
    raw = (raw or "").strip()
    if re.fullmatch(r"\d+", raw):
        return raw
    patterns = [
        r"/reel/(\d+)",
        r"/posts/(\d+)",
        r"/videos/(\d+)",
        r"[?&]fbid=(\d+)",
        r"story_fbid=(\d+)",
        r"[?&]v=(\d+)",
    ]
    for p in patterns:
        m = re.search(p, raw)
        if m:
            return m.group(1)
    if "/share/" in raw:
        raise RuntimeError(
            "Share links (/share/...) can't be resolved by the API. "
            "Open the reel in your browser, copy the URL from the address bar "
            "(it looks like facebook.com/reel/123456...), and paste that instead."
        )
    raise RuntimeError(
        "Could not find a post ID in that link. Paste the full reel/post URL "
        "from your browser's address bar, or the numeric post ID."
    )


# ---------------------------------------------------------------- dashboard password gate

_GATE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Password required</title>
<style>body{font-family:system-ui,sans-serif;background:#f0f2f5;display:flex;justify-content:center;padding-top:80px;margin:0}
.card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.15);width:320px}
input{width:100%;padding:11px;border:1px solid #ccd0d5;border-radius:8px;margin:10px 0;box-sizing:border-box;font-size:15px}
button{background:#1877f2;color:#fff;border:none;border-radius:8px;padding:11px 20px;cursor:pointer;width:100%;font-size:15px}
.err{color:#b3261e;font-size:14px;min-height:18px}</style></head>
<body><div class="card"><h3>&#128274; Dashboard password</h3>
<form method="post" action="/gate"><input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">Open dashboard</button></form>
<p class="err">__ERR__</p></div></body></html>"""


@app.before_request
def _gate_guard():
    if not DASHBOARD_PASSWORD or session.get("gate_ok"):
        return None
    if request.endpoint in ("gate", "static"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Dashboard password required. Open /gate first."}), 401
    return redirect("/gate")


@app.route("/gate", methods=["GET", "POST"])
def gate():
    err = ""
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD):
            session["gate_ok"] = True
            return redirect("/")
        err = "Wrong password."
    return _GATE_HTML.replace("__ERR__", err)


# ---------------------------------------------------------------- pages

@app.route("/")
def index():
    return render_template(
        "index.html",
        configured=bool(APP_ID and APP_SECRET),
        app_id=APP_ID,
    )


@app.route("/api/config")
def api_config():
    return jsonify({"configured": bool(APP_ID and APP_SECRET)})


@app.route("/api/login")
def api_login():
    if not (APP_ID and APP_SECRET):
        return jsonify({"error": "FB_APP_ID / FB_APP_SECRET not set. See README.md."}), 500
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = urlencode({
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    })
    return redirect(f"https://www.facebook.com/v22.0/dialog/oauth?{params}")


@app.route("/api/callback")
def api_callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "Invalid state. Try logging in again.", 400
    code = request.args.get("code")
    if not code:
        return f"Login failed: {request.args.get('error_description', 'no code returned')}", 400

    # code -> short-lived user token
    data = graph_get("/oauth/access_token", {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "client_secret": APP_SECRET,
        "code": code,
    })
    short_token = data["access_token"]

    # short-lived -> long-lived user token (valid ~60 days)
    data = graph_get("/oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": short_token,
    })
    session["user_token"] = data["access_token"]
    # persist server-side so the watcher survives browser sessions/restarts
    with STORE_LOCK:
        store = load_store()
        store["user_token"] = data["access_token"]
        save_store(store)
    session.pop("page_token", None)
    session.pop("page_id", None)
    return redirect("/")


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    clear_store()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    _restore_session_from_store()
    tok = session.get("user_token")
    if not tok:
        return jsonify({"logged_in": False})
    try:
        me = graph_get("/me", {"access_token": tok, "fields": "id,name"})
        return jsonify({"logged_in": True, "name": me.get("name")})
    except RuntimeError as e:
        session.clear()
        clear_store()
        return jsonify({"logged_in": False, "error": str(e)})


@app.route("/api/pages")
def api_pages():
    _restore_session_from_store()
    tok = session.get("user_token")
    if not tok:
        return jsonify({"error": "Not logged in"}), 401
    data = graph_get("/me/accounts", {"access_token": tok, "fields": "id,name"})
    pages = [{"id": p["id"], "name": p.get("name", p["id"])} for p in data.get("data", [])]
    # stash page tokens server-side; only ids go to the browser
    tokens = {p["id"]: p["access_token"] for p in data.get("data", []) if p.get("access_token")}
    session["page_tokens"] = tokens
    # persist tokens in the server-side store — the watcher reads them from
    # here, never from the browser session
    names = {p["id"]: p.get("name", p["id"]) for p in data.get("data", [])}
    with STORE_LOCK:
        store = load_store()
        store["user_token"] = tok
        store["pages"] = {
            pid: {"name": names.get(pid, pid), "access_token": t}
            for pid, t in tokens.items()
        }
        save_store(store)
    return jsonify({"pages": pages})


@app.route("/api/select-page", methods=["POST"])
def api_select_page():
    body = request.get_json(force=True)
    pid = str(body.get("page_id", ""))
    tok = (session.get("page_tokens") or {}).get(pid)
    if not tok:
        # fall back to the server-side store (e.g. session was restored)
        page = (load_store().get("pages") or {}).get(pid) or {}
        tok = page.get("access_token")
    if not tok:
        return jsonify({"error": "Unknown page. List pages again."}), 400
    session["page_id"] = pid
    session["page_token"] = tok
    with STORE_LOCK:
        store = load_store()
        store["selected_page_id"] = pid
        save_store(store)
    return jsonify({"ok": True})



# ---------------------------------------------------------------- comments

@app.route("/api/comments", methods=["POST"])
def api_comments():
    try:
        body = request.get_json(force=True)
        post_id = resolve_post_id(body.get("post_input", ""))
        hours = float(body.get("hours", 48))
        tok = page_token()
        page_id = session.get("page_id")

        # sanity check: post exists and is visible to this page token
        try:
            post = graph_get(f"/{post_id}", {
                "access_token": tok, "fields": "id,from{id,name}"})
        except RuntimeError as e:
            return jsonify({"error": f"Can't read that post: {e}. "
                                     "Make sure it belongs to the selected Page."}), 400

        comments = []
        url = f"{GRAPH}/{post_id}/comments"
        params = {
            "access_token": tok,
            "fields": "id,message,created_time,from{id,name}",
            "limit": 100,
        }
        while url:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message", "Graph API error"))
            comments.extend(data.get("data", []))
            url = (data.get("paging") or {}).get("next")
            params = {}  # next already contains everything

        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        fresh = []
        for c in comments:
            try:
                ts = datetime.fromisoformat(c["created_time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            if str((c.get("from") or {}).get("id")) == str(page_id):
                continue  # skip the Page's own comments
            fresh.append({
                "id": c["id"],
                "message": c.get("message", ""),
                "from_name": (c.get("from") or {}).get("name", "?"),
                "created_time": c["created_time"],
            })

        state = load_state()
        already = set(state.get(post_id, []))
        todo = [c for c in fresh if c["id"] not in already]

        return jsonify({
            "post_id": post_id,
            "total": len(comments),
            "fresh": len(fresh),
            "already_replied": len(fresh) - len(todo),
            "comments": todo,
        })
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/send-one", methods=["POST"])
def api_send_one():
    """Send a single reply. The frontend waits `delay` between calls."""
    try:
        body = request.get_json(force=True)
        comment_id = str(body.get("comment_id", ""))
        post_id = str(body.get("post_id", ""))
        text = (body.get("text") or "").strip()
        if not comment_id or not text:
            return jsonify({"error": "Missing comment_id or text"}), 400
        tok = page_token()

        with STATE_LOCK:
            state = load_state()
            if comment_id in set(state.get(post_id, [])):
                return jsonify({"ok": True, "skipped": True, "reason": "already replied"})

            res = graph_post(f"/{comment_id}/comments", {
                "access_token": tok, "message": text})
            state.setdefault(post_id, []).append(comment_id)
            save_state(state)
        return jsonify({"ok": True, "reply_id": res.get("id")})
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ---------------------------------------------------------------- auto watch (multi-post)
#
# The watch list lives in watches.json: [{page_id, post_id, text, delay,
# active, started_ts, sent_total}]. ONE background manager thread cycles
# through all ACTIVE watches every POLL_INTERVAL seconds and auto-replies
# to comments that are top-level (the /{post}/comments edge only returns
# top-level), not from the Page itself, created at/after the watch's
# started_ts, and not already in sent_replies.json. Tokens come from
# fb_store.json — never from the browser session — so watching continues
# with the browser closed, as long as this app process stays running.

MANAGER = {"thread": None, "stop": threading.Event()}
WATCH_STATS = {}  # "page_id:post_id" -> {sent, errors, waiting, last_check, last_error, last_reply_to}


def _watch_key(w):
    return f"{w['page_id']}:{w['post_id']}"


def _stats_for(key):
    return WATCH_STATS.setdefault(key, {
        "sent": 0, "errors": 0, "waiting": 0,
        "last_check": None, "last_error": None, "last_reply_to": None,
    })


def _fetch_all_comments(post_id, tok):
    comments = []
    url = f"{GRAPH}/{post_id}/comments"
    params = {
        "access_token": tok,
        "fields": "id,message,created_time,from{id,name}",
        "limit": 100,
    }
    while url:
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "Graph API error"))
        comments.extend(data.get("data", []))
        url = (data.get("paging") or {}).get("next")
        params = {}
    return comments


def _watch_still_active(w):
    for ww in load_watches():
        if (str(ww["page_id"]) == str(w["page_id"])
                and str(ww["post_id"]) == str(w["post_id"])):
            return bool(ww.get("active"))
    return False  # deleted


def _bump_sent_total(w):
    with WATCH_LOCK:
        watches = load_watches()
        for ww in watches:
            if (str(ww["page_id"]) == str(w["page_id"])
                    and str(ww["post_id"]) == str(w["post_id"])):
                ww["sent_total"] = ww.get("sent_total", 0) + 1
        save_watches(watches)


def _page_already_replied(comment_id, page_id, tok):
    """Stateless dedup: ask Facebook itself whether the Page has already
    replied to this comment. This keeps working even if sent_replies.json
    is wiped (free hosts like Render have an ephemeral disk), so a restart
    can never cause a duplicate reply — at most one extra API call per
    genuinely new comment."""
    data = graph_get(f"/{comment_id}/comments", {
        "access_token": tok, "fields": "from{id}", "limit": 25})
    for r in data.get("data", []):
        if str((r.get("from") or {}).get("id")) == str(page_id):
            return True
    return False


def _process_watch(w):
    stats = _stats_for(_watch_key(w))
    store = load_store()
    page = (store.get("pages") or {}).get(str(w["page_id"])) or {}
    tok = page.get("access_token")
    if not tok:
        stats["errors"] += 1
        stats["last_error"] = ("Page token missing from store. Login again "
                               "and open the dashboard once so tokens re-save.")
        return
    comments = _fetch_all_comments(w["post_id"], tok)
    stats["last_check"] = datetime.now(timezone.utc).isoformat()
    with STATE_LOCK:
        done = set(load_state().get(w["post_id"], []))
    fresh = []
    for c in comments:
        if str((c.get("from") or {}).get("id")) == str(w["page_id"]):
            continue  # the Page's own comments
        if c["id"] in done:
            continue  # already replied earlier
        try:
            ts = datetime.fromisoformat(
                c["created_time"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if ts < float(w.get("started_ts") or 0):
            continue  # only comments that arrive after the watch was added
        fresh.append(c)
    fresh.sort(key=lambda c: c["created_time"])  # oldest first
    stats["waiting"] = len(fresh)
    try:
        delay = max(20, int(w.get("delay", 60)))
    except (TypeError, ValueError):
        delay = 60
    for i, c in enumerate(fresh):
        if MANAGER["stop"].is_set() or not _watch_still_active(w):
            break
        try:
            already = _page_already_replied(c["id"], w["page_id"], tok)
        except RuntimeError as e:
            # check failed — skip this cycle rather than risk a duplicate
            stats["errors"] += 1
            stats["last_error"] = f"dedup check {c['id']}: {e}"
            continue
        if already:
            with STATE_LOCK:
                state = load_state()
                state.setdefault(w["post_id"], []).append(c["id"])
                save_state(state)
            stats["waiting"] = max(0, stats["waiting"] - 1)
            continue
        try:
            graph_post(f"/{c['id']}/comments", {
                "access_token": tok, "message": w["text"]})
        except RuntimeError as e:
            stats["errors"] += 1
            stats["last_error"] = f"reply to {c['id']}: {e}"
            continue
        with STATE_LOCK:
            state = load_state()
            state.setdefault(w["post_id"], []).append(c["id"])
            save_state(state)
        _bump_sent_total(w)
        stats["sent"] += 1
        stats["waiting"] = max(0, stats["waiting"] - 1)
        stats["last_reply_to"] = (c.get("from") or {}).get("name", "?")
        if i < len(fresh) - 1:
            MANAGER["stop"].wait(delay)


def _manager_loop():
    while not MANAGER["stop"].is_set():
        try:
            for w in load_watches():
                if MANAGER["stop"].is_set():
                    break
                if not w.get("active"):
                    continue
                try:
                    _process_watch(w)
                except Exception as e:  # one bad watch must not kill the cycle
                    stats = _stats_for(_watch_key(w))
                    stats["errors"] += 1
                    stats["last_error"] = str(e)
        except Exception:
            pass  # transient file errors etc. — try again next cycle
        MANAGER["stop"].wait(POLL_INTERVAL)


def ensure_manager():
    t = MANAGER.get("thread")
    if t is not None and t.is_alive():
        return
    t = threading.Thread(target=_manager_loop, daemon=True)
    MANAGER["thread"] = t
    t.start()


def _watch_view(w, index, pages):
    return {
        "index": index,
        "page_id": w["page_id"],
        "page_name": (pages.get(str(w["page_id"])) or {}).get("name"),
        "post_id": w["post_id"],
        "text": w["text"],
        "delay": w.get("delay", 60),
        "active": bool(w.get("active")),
        "started_ts": w.get("started_ts"),
        "sent_total": w.get("sent_total", 0),
        "stats": dict(WATCH_STATS.get(_watch_key(w), {})),
    }


@app.route("/api/watches")
def api_watches_list():
    pages = load_store().get("pages") or {}
    watches = [_watch_view(w, i, pages) for i, w in enumerate(load_watches())]
    t = MANAGER.get("thread")
    return jsonify({
        "running": bool(t is not None and t.is_alive()),
        "poll_interval": POLL_INTERVAL,
        "watches": watches,
    })


@app.route("/api/watches", methods=["POST"])
def api_watch_add():
    try:
        body = request.get_json(silent=True) or {}
        post_id = resolve_post_id(body.get("post_input", ""))
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Reply text is empty."}), 400
        try:
            delay = max(20, int(body.get("delay", 60)))
        except (TypeError, ValueError):
            delay = 60
        store = load_store()
        pages = store.get("pages") or {}
        page_id = str(body.get("page_id") or session.get("page_id")
                      or store.get("selected_page_id") or "")
        if not page_id and len(pages) == 1:
            page_id = next(iter(pages))
        tok = (pages.get(page_id) or {}).get("access_token")
        if not tok:
            return jsonify({"error": "No saved token for that Page. Login "
                                     "with Facebook first and open the Pages "
                                     "list once, so the token gets saved."}), 400
        try:  # the post must be readable with this Page's token
            graph_get(f"/{post_id}", {"access_token": tok, "fields": "id"})
        except RuntimeError as e:
            return jsonify({"error": f"Can't read that post: {e}. "
                                     "Make sure it belongs to the selected Page."}), 400
        with WATCH_LOCK:
            watches = load_watches()
            for w in watches:
                if (str(w["page_id"]) == page_id
                        and str(w["post_id"]) == str(post_id)):
                    return jsonify({"error": "That post is already in the watch list."}), 400
            watches.append({
                "page_id": page_id,
                "post_id": str(post_id),
                "text": text,
                "delay": delay,
                "active": True,
                "started_ts": time.time(),
                "sent_total": 0,
            })
            save_watches(watches)
            index = len(watches) - 1
        ensure_manager()
        return jsonify({"ok": True, "index": index, "post_id": str(post_id)})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/watches/<int:index>/toggle", methods=["POST"])
def api_watch_toggle(index):
    with WATCH_LOCK:
        watches = load_watches()
        if index >= len(watches):
            return jsonify({"error": "Watch not found."}), 404
        # NOTE: started_ts is NOT touched — resuming never backfills old comments
        watches[index]["active"] = not watches[index].get("active")
        save_watches(watches)
        active = watches[index]["active"]
    if active:
        ensure_manager()
    return jsonify({"ok": True, "active": active})


@app.route("/api/watches/<int:index>/delete", methods=["POST"])
def api_watch_delete(index):
    with WATCH_LOCK:
        watches = load_watches()
        if index >= len(watches):
            return jsonify({"error": "Watch not found."}), 404
        watches.pop(index)
        save_watches(watches)
    return jsonify({"ok": True})


@app.route("/api/watch/status")
def api_watch_status():
    """Summary only (the old single-post watch endpoints are gone)."""
    watches = load_watches()
    t = MANAGER.get("thread")
    return jsonify({
        "running": bool(t is not None and t.is_alive()),
        "watches": len(watches),
        "active": sum(1 for w in watches if w.get("active")),
        "sent": sum(s.get("sent", 0) for s in WATCH_STATS.values()),
    })


# start the manager at boot if a previous run left active watches behind
try:
    if any(w.get("active") for w in load_watches()):
        ensure_manager()
except Exception:
    pass


# ---------------------------------------------------------------- run

if __name__ == "__main__":
    if not (APP_ID and APP_SECRET):
        print("! FB_APP_ID / FB_APP_SECRET not set — see README.md, then restart.")
    _port = os.environ.get("PORT", "").strip()
    if _port:
        try:
            app.run(host="0.0.0.0", port=int(_port), debug=False, threaded=True)
        except ValueError:
            app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
    else:
        app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
