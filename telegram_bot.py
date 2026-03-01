"""
telegram_bot.py — Bot Telegram "Listen & Mine" — versione PRODUCTION
---------------------------------------------------------------------
Deploy su Railway.app
Le credenziali vengono lette dalle variabili d'ambiente di Railway,
NON da config.json (che non esiste in produzione).
"""

import asyncio
import os
import sqlite3
import threading
import time
import base64
import secrets
import urllib.parse
import requests
import logging

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# -------------------------------------------------------
# LOGGING
# -------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# -------------------------------------------------------
# CONFIG — tutto da variabili d'ambiente Railway
# -------------------------------------------------------
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLIENT_ID       = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET   = os.environ["SPOTIFY_CLIENT_SECRET"]
# Railway fornisce l'URL pubblico in RAILWAY_PUBLIC_DOMAIN
_domain         = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PUBLIC_URL      = f"https://{_domain}" if _domain else os.environ.get("PUBLIC_URL", "")
CALLBACK_PORT   = int(os.environ.get("PORT", 8082))
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL_SEC", 5))

REDIRECT_URI     = f"{PUBLIC_URL}/callback"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL= "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SCOPE = (
    "user-read-playback-state user-read-currently-playing "
    "playlist-read-private playlist-read-collaborative "
    "user-modify-playback-state"
)

log.info(f"PUBLIC_URL={PUBLIC_URL}")
log.info(f"REDIRECT_URI={REDIRECT_URI}")
log.info(f"PORT={CALLBACK_PORT}")

# -------------------------------------------------------
# DATABASE SQLite
# -------------------------------------------------------
DB_FILE  = "bot_users.db"
_db_lock = threading.Lock()

def db_connect():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with _db_lock, db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id   INTEGER PRIMARY KEY,
                username      TEXT    DEFAULT '',
                access_token  TEXT,
                refresh_token TEXT,
                expires_in    INTEGER DEFAULT 3600,
                token_at      INTEGER DEFAULT 0,
                mining_active INTEGER DEFAULT 0,
                last_track    TEXT    DEFAULT ''
            )
        """)
        conn.commit()

def db_get(tid: int) -> dict | None:
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
        return dict(row) if row else None

def db_set(tid: int, **kw):
    with _db_lock, db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (tid,))
        if kw:
            sets = ", ".join(f"{k}=?" for k in kw)
            conn.execute(
                f"UPDATE users SET {sets} WHERE telegram_id=?",
                list(kw.values()) + [tid]
            )
        conn.commit()

def db_active_users() -> list[dict]:
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE access_token IS NOT NULL AND mining_active=1"
        ).fetchall()
        return [dict(r) for r in rows]

# -------------------------------------------------------
# SPOTIFY UTILITIES
# -------------------------------------------------------
def now_ts() -> int:
    return int(time.time())

def token_valid(user: dict) -> bool:
    if not user.get("access_token") or not user.get("token_at"):
        return False
    exp = int(user.get("expires_in") or 0)
    return exp <= 0 or (now_ts() - user["token_at"]) < (exp - 60)

def do_refresh(user: dict) -> bool:
    rt = user.get("refresh_token")
    if not rt:
        return False
    b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r   = requests.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": rt},
        headers={"Authorization": f"Basic {b64}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200:
        return False
    d = r.json()
    db_set(user["telegram_id"],
           access_token  = d["access_token"],
           refresh_token = d.get("refresh_token", rt),
           expires_in    = d.get("expires_in", 3600),
           token_at      = now_ts())
    return True

def valid_token(user: dict) -> str | None:
    if not token_valid(user):
        if not do_refresh(user):
            return None
        user = db_get(user["telegram_id"])
    return user.get("access_token")

def sp_get(user: dict, path: str, params: dict = None) -> dict | None:
    tok = valid_token(user)
    if not tok:
        return None
    r = requests.get(
        SPOTIFY_API_BASE + path,
        headers={"Authorization": f"Bearer {tok}"},
        params=params or {},
        timeout=10,
    )
    if r.status_code == 204:
        return {"_204": True}
    if r.status_code >= 400:
        return {"_err": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}

def sp_put(user: dict, path: str, params: dict = None, body=None) -> dict | None:
    tok = valid_token(user)
    if not tok:
        return None
    r = requests.put(
        SPOTIFY_API_BASE + path,
        headers={"Authorization": f"Bearer {tok}"},
        params=params or {},
        json=body,
        timeout=10,
    )
    return {"_status": r.status_code}

def sp_post(user: dict, path: str) -> dict | None:
    tok = valid_token(user)
    if not tok:
        return None
    r = requests.post(
        SPOTIFY_API_BASE + path,
        headers={"Authorization": f"Bearer {tok}"},
        timeout=10,
    )
    return {"_status": r.status_code}

# -------------------------------------------------------
# OAUTH SERVER (Flask — gestisce il callback Spotify)
# -------------------------------------------------------
_oauth_app     = Flask("oauth")
_pending: dict = {}
_tg_app        = None

@_oauth_app.get("/")
def oauth_home():
    return (
        "<h3>✅ SpoteeBeeBot — Server attivo</h3>"
        f"<p>Redirect URI: {REDIRECT_URI}</p>"
        f"<p>Stati OAuth in attesa: {len(_pending)}</p>"
    )

@_oauth_app.get("/health")
def health():
    return {"status": "ok", "redirect_uri": REDIRECT_URI}

@_oauth_app.get("/callback")
def oauth_cb():
    err   = request.args.get("error", "")
    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    log.info(f"[OAuth] callback ricevuto — code={'OK' if code else 'MANCANTE'} state={state!r}")

    if err:
        return f"<h3>Errore Spotify: {err}</h3>Torna su Telegram e riprova.", 400
    if not code:
        return "<h3>Codice mancante.</h3>Torna su Telegram e riprova.", 400

    tid = _pending.pop(state, None)
    if not tid:
        # Fallback: prendi l'ultimo utente senza token
        with _db_lock, db_connect() as conn:
            row = conn.execute(
                "SELECT telegram_id FROM users WHERE access_token IS NULL ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        tid = row["telegram_id"] if row else None

    if not tid:
        return "<h3>Sessione scaduta.</h3>Torna su Telegram → premi Connetti Spotify.", 400

    b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r   = requests.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": REDIRECT_URI},
        headers={"Authorization": f"Basic {b64}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )

    if r.status_code != 200:
        log.error(f"[OAuth] Errore token Spotify: {r.status_code} {r.text}")
        return f"<h3>Errore Spotify ({r.status_code})</h3>Riprova da Telegram.", 500

    d = r.json()
    db_set(tid,
           access_token  = d["access_token"],
           refresh_token = d.get("refresh_token", ""),
           expires_in    = d.get("expires_in", 3600),
           token_at      = now_ts(),
           mining_active = 1)

    log.info(f"[OAuth] ✅ Token salvato per telegram_id={tid}")

    threading.Thread(target=_async_notify, args=(tid,
        "✅ *Spotify connesso!*\n\n"
        "Il bot inizia subito a monitorare la tua musica 🎵\n"
        "Appena ascolti qualcosa il mining parte in automatico.\n\n"
        "Usa /menu per i controlli."
    ), daemon=True).start()

    return """<html><body style="font-family:sans-serif;text-align:center;padding:50px;max-width:500px;margin:auto">
    <h2>✅ Spotify connesso!</h2>
    <p style="font-size:18px">Torna su Telegram — il bot è già attivo.</p>
    <p><b>Puoi chiudere questa finestra.</b></p>
    </body></html>"""

def _async_notify(tid: int, text: str):
    time.sleep(1)
    if _tg_app:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_send(tid, text))
            loop.close()
        except Exception as e:
            log.error(f"Notify error: {e}")

def _run_async(coro):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro)
        loop.close()
    except Exception as e:
        log.error(f"Async error: {e}")

def _start_oauth_server():
    _oauth_app.run("0.0.0.0", CALLBACK_PORT, debug=False, use_reloader=False)

# -------------------------------------------------------
# MINING MONITOR
# -------------------------------------------------------
def mining_monitor():
    log.info("Mining monitor avviato.")
    while True:
        try:
            for user in db_active_users():
                _poll_user(user)
        except Exception as e:
            log.error(f"Monitor error: {e}")
        time.sleep(POLL_INTERVAL)

def _poll_user(user: dict):
    tid  = user["telegram_id"]
    data = sp_get(user, "/me/player/currently-playing",
                  params={"additional_types": "track,episode"})

    if data is None:
        db_set(tid, mining_active=0)
        _run_async(_send(tid, "⚠️ Token Spotify scaduto.\nUsa /start per riconnetterti."))
        return

    if data.get("_204") or not data.get("is_playing"):
        if user.get("last_track"):
            db_set(tid, last_track="")
            _run_async(_send(tid, "⏹️ Musica ferma — mining in pausa."))
        return

    item   = data.get("item") or {}
    track  = item.get("name", "")
    artist = ", ".join(a["name"] for a in item.get("artists", []))
    label  = f"{artist} — {track}" if track else "brano sconosciuto"

    if label != user.get("last_track", ""):
        db_set(tid, last_track=label)
        _run_async(_send(tid, f"🎵 *{label}*\n⛏️ Mining avviato!"))

# -------------------------------------------------------
# TELEGRAM — send helper
# -------------------------------------------------------
async def _send(tid: int, text: str, markup=None):
    if not _tg_app:
        return
    try:
        await _tg_app.bot.send_message(
            chat_id=tid, text=text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception as e:
        log.error(f"Send error a {tid}: {e}")

# -------------------------------------------------------
# KEYBOARDS
# -------------------------------------------------------
def main_kb(user: dict | None) -> InlineKeyboardMarkup:
    authed = bool(user and user.get("access_token"))
    mining = bool(user and user.get("mining_active"))
    rows   = []
    if not authed:
        rows.append([InlineKeyboardButton("🎵 Connetti Spotify", callback_data="connect")])
    else:
        rows.append([
            InlineKeyboardButton("🔄 Stato",    callback_data="status"),
            InlineKeyboardButton("📋 Playlist", callback_data="playlists"),
        ])
        rows.append([InlineKeyboardButton(
            "⏸️ Sospendi mining" if mining else "▶️ Riprendi mining",
            callback_data="mining_off" if mining else "mining_on"
        )])
        rows.append([
            InlineKeyboardButton("▶️ Play",  callback_data="play"),
            InlineKeyboardButton("⏸️ Pause", callback_data="pause"),
            InlineKeyboardButton("⏭️ Next",  callback_data="next"),
        ])
        rows.append([InlineKeyboardButton("🔌 Disconnetti", callback_data="disconnect")])
    return InlineKeyboardMarkup(rows)

# -------------------------------------------------------
# HANDLERS
# -------------------------------------------------------
async def h_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid  = update.effective_user.id
    name = update.effective_user.first_name or "utente"
    db_set(tid, username=update.effective_user.username or "")
    user   = db_get(tid)
    authed = bool(user and user.get("access_token"))

    if authed:
        txt = f"👋 Bentornato *{name}*!\nIl bot monitora Spotify automaticamente. Cosa faccio?"
    else:
        txt = (
            f"👋 Ciao *{name}*!\n\n"
            "Questo bot collega Spotify al mining Acki Nacki.\n\n"
            "🎵 *Come funziona:*\n"
            "1. Premi *Connetti Spotify*\n"
            "2. Accedi con il tuo account\n"
            "3. Ascolta musica — il mining parte da solo ✅\n\n"
            "Premi il bottone qui sotto 👇"
        )
    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb(user))

async def h_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = db_get(update.effective_user.id)
    await update.message.reply_text("📱 *Menu*", parse_mode="Markdown", reply_markup=main_kb(user))

async def h_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    tid  = update.effective_user.id
    data = q.data
    user = db_get(tid)

    if data == "connect":
        state          = secrets.token_urlsafe(16)
        _pending[state] = tid
        params = {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "state": state, "show_dialog": "true",
        }
        url = SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(params)
        await q.edit_message_text(
            "🔐 *Connetti Spotify in 3 passi:*\n\n"
            "1️⃣ Premi il link qui sotto\n"
            "2️⃣ Accedi con il tuo account Spotify\n"
            "3️⃣ Clicca *Accetta* → torna qui ✅\n\n"
            f"👉 [Apri Spotify e autorizza]({url})",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Ho autorizzato, controlla", callback_data="check_auth")
            ]])
        )

    elif data == "check_auth":
        user = db_get(tid)
        if user and user.get("access_token"):
            await q.edit_message_text(
                "✅ *Connesso!*\nMining automatico attivo 🚀",
                parse_mode="Markdown", reply_markup=main_kb(user)
            )
        else:
            await q.edit_message_text(
                "⏳ Non ho ancora ricevuto l'autorizzazione.\n"
                "Assicurati di aver premuto *Accetta* su Spotify.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Riprova",    callback_data="check_auth"),
                    InlineKeyboardButton("🔙 Ricomincia", callback_data="connect"),
                ]])
            )

    elif data == "status":
        await _edit_status(q, user)

    elif data == "mining_on":
        db_set(tid, mining_active=1)
        await q.edit_message_text("▶️ *Mining riattivato!*",
            parse_mode="Markdown", reply_markup=main_kb(db_get(tid)))

    elif data == "mining_off":
        db_set(tid, mining_active=0, last_track="")
        await q.edit_message_text("⏸️ *Mining sospeso.*",
            parse_mode="Markdown", reply_markup=main_kb(db_get(tid)))

    elif data == "play":
        res = sp_put(user, "/me/player/play")
        await q.answer(f"▶️ Play ({res['_status'] if res else 'err'})")
    elif data == "pause":
        res = sp_put(user, "/me/player/pause")
        await q.answer(f"⏸️ Pause ({res['_status'] if res else 'err'})")
    elif data == "next":
        res = sp_post(user, "/me/player/next")
        await q.answer(f"⏭️ Next ({res['_status'] if res else 'err'})")

    elif data == "playlists":
        await _edit_playlists(q, user)

    elif data.startswith("pl:"):
        uri = data[3:]
        res = sp_put(user, "/me/player/play", body={"context_uri": uri})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Playlist avviata!")
        else:
            await q.answer("⚠️ Serve Spotify Premium e un device attivo.", show_alert=True)

    elif data == "disconnect":
        db_set(tid, access_token=None, refresh_token=None,
               mining_active=0, last_track="")
        await q.edit_message_text(
            "🔌 *Disconnesso da Spotify.*\nUsa /start per riconnetterti.",
            parse_mode="Markdown"
        )

    elif data == "back":
        await q.edit_message_text("📱 *Menu*", parse_mode="Markdown",
            reply_markup=main_kb(db_get(tid)))

# -------------------------------------------------------
# Helper: stato corrente
# -------------------------------------------------------
async def _edit_status(q, user):
    if not user or not user.get("access_token"):
        await q.edit_message_text("❌ Non connesso. Usa /start.")
        return
    data = sp_get(user, "/me/player/currently-playing",
                  params={"additional_types": "track,episode"})
    if data is None:
        txt = "⚠️ Token scaduto. Usa /start."
    elif data.get("_204") or not data.get("is_playing"):
        m   = "⛏️ Mining attivo" if user.get("mining_active") else "⏸️ Mining sospeso"
        txt = f"⏹️ *Niente in riproduzione*\n{m}"
    else:
        item   = data.get("item") or {}
        track  = item.get("name", "?")
        artist = ", ".join(a["name"] for a in item.get("artists", []))
        prog   = data.get("progress_ms", 0)
        dur    = max(item.get("duration_ms", 1), 1)
        pct    = int(prog / dur * 100)
        bar    = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        m      = "⛏️ Mining attivo" if user.get("mining_active") else "⏸️ Mining sospeso"
        txt    = f"▶️ *{artist} — {track}*\n{bar} {pct}%\n\n{m}"
    await q.edit_message_text(txt, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aggiorna", callback_data="status"),
            InlineKeyboardButton("🔙 Menu",     callback_data="back"),
        ]]))

# -------------------------------------------------------
# Helper: playlist
# -------------------------------------------------------
async def _edit_playlists(q, user):
    if not user:
        await q.edit_message_text("❌ Non connesso.")
        return
    data  = sp_get(user, "/me/playlists", params={"limit": 10})
    items = (data or {}).get("items", [])
    back  = [[InlineKeyboardButton("🔙 Menu", callback_data="back")]]
    if not items:
        await q.edit_message_text("📋 Nessuna playlist trovata.",
            reply_markup=InlineKeyboardMarkup(back))
        return
    rows = []
    for p in items[:8]:
        name  = p.get("name", "?")
        total = p.get("tracks", {}).get("total", "?")
        uri   = p.get("uri", "")
        rows.append([InlineKeyboardButton(
            f"▶️ {name} ({total})", callback_data=f"pl:{uri}"
        )])
    rows += back
    await q.edit_message_text("📋 *Le tue playlist:*", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows))

# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
def main():
    global _tg_app

    if not PUBLIC_URL:
        log.error("❌ PUBLIC_URL non impostato! Imposta RAILWAY_PUBLIC_DOMAIN o PUBLIC_URL.")
        return

    db_init()

    # Flask OAuth server in background
    threading.Thread(target=_start_oauth_server, daemon=True).start()
    log.info(f"OAuth server avviato su porta {CALLBACK_PORT}")

    # Mining monitor in background
    threading.Thread(target=mining_monitor, daemon=True).start()

    # Bot Telegram
    _tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    _tg_app.add_handler(CommandHandler("start", h_start))
    _tg_app.add_handler(CommandHandler("menu",  h_menu))
    _tg_app.add_handler(CallbackQueryHandler(h_button))

    log.info("✅ Bot avviato!")
    _tg_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
