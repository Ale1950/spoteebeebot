"""
telegram_bot.py v2.0 — Bot Telegram "Listen & Mine"
----------------------------------------------------
Avvio:  python telegram_bot.py
Dipendenze: pip install python-telegram-bot flask requests

Novità v2.0:
- Statistiche mining (sessioni, brani, tempo totale)
- Notifiche brano più belle con progress bar e album
- Messaggio di benvenuto professionale
- Comando /stats con dashboard completa
- Notifica riassuntiva giornaliera (ore 21:00)
- Lista playlist con possibilità di vedere i brani
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
import base64
import secrets
import urllib.parse
import requests
import logging
from datetime import datetime, date

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
# CONFIG
# -------------------------------------------------------
CONFIG_FILE = "config.json"

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        print("\n" + "="*50)
        print("  PRIMA CONFIGURAZIONE — Listen & Mine Bot")
        print("="*50)
        print("\nHai bisogno del token del tuo bot Telegram.")
        print("Se non ce l'hai ancora:")
        print("  1. Apri Telegram → cerca @BotFather")
        print("  2. Scrivi /newbot → segui le istruzioni")
        print("  3. Copia il token e incollalo qui")
        token = input("\nIncolla qui il token del bot: ").strip()
        print("\nCredenziali Spotify (premi INVIO per usare quelle di default):")
        cid  = input("  Client ID    [invio = default]: ").strip()
        csec = input("  Client Secret [invio = default]: ").strip()
        cfg = {
            "TELEGRAM_TOKEN":        token,
            "SPOTIFY_CLIENT_ID":     cid  or "21675318528e48c9a7a5c85b1f53da54",
            "SPOTIFY_CLIENT_SECRET": csec or "b582f0666fe9434ebd54f691a0d2d3b4",
            "OAUTH_CALLBACK_PORT":   8082,
            "POLL_INTERVAL_SEC":     5,
            "DAILY_SUMMARY_HOUR":    21,
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"\nConfigurazione salvata in {CONFIG_FILE} ✅\n")
        return cfg
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

CFG                  = load_config()
TELEGRAM_TOKEN       = CFG["TELEGRAM_TOKEN"]
CLIENT_ID            = CFG["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET        = CFG["SPOTIFY_CLIENT_SECRET"]
CALLBACK_PORT        = int(CFG.get("OAUTH_CALLBACK_PORT", 8082))
POLL_INTERVAL        = int(CFG.get("POLL_INTERVAL_SEC", 5))
DAILY_SUMMARY_HOUR   = int(CFG.get("DAILY_SUMMARY_HOUR", 21))

REDIRECT_URI     = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL= "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SCOPE = (
    "user-read-playback-state user-read-currently-playing "
    "playlist-read-private playlist-read-collaborative "
    "user-modify-playback-state"
)

# -------------------------------------------------------
# DATABASE — utenti + statistiche
# -------------------------------------------------------
DB_FILE  = "bot_users.db"
_db_lock = threading.Lock()

def db_connect():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with _db_lock, db_connect() as conn:
        # Tabella utenti
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id       INTEGER PRIMARY KEY,
                username          TEXT    DEFAULT '',
                first_name        TEXT    DEFAULT '',
                access_token      TEXT,
                refresh_token     TEXT,
                expires_in        INTEGER DEFAULT 3600,
                token_at          INTEGER DEFAULT 0,
                mining_active     INTEGER DEFAULT 0,
                last_track        TEXT    DEFAULT '',
                joined_at         INTEGER DEFAULT 0
            )
        """)
        # Tabella statistiche
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                telegram_id       INTEGER,
                stat_date         TEXT,
                sessions          INTEGER DEFAULT 0,
                tracks_heard      INTEGER DEFAULT 0,
                mining_minutes    INTEGER DEFAULT 0,
                PRIMARY KEY (telegram_id, stat_date)
            )
        """)
        conn.commit()
    log.info("Database inizializzato.")

def db_get(tid: int) -> dict | None:
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
        return dict(row) if row else None

def db_set(tid: int, **kw):
    with _db_lock, db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id, joined_at) VALUES (?,?)",
                     (tid, now_ts()))
        if kw:
            sets = ", ".join(f"{k}=?" for k in kw)
            conn.execute(f"UPDATE users SET {sets} WHERE telegram_id=?",
                         list(kw.values()) + [tid])
        conn.commit()

def db_active_users() -> list[dict]:
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE access_token IS NOT NULL AND mining_active=1"
        ).fetchall()
        return [dict(r) for r in rows]

def db_all_with_token() -> list[dict]:
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE access_token IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]

# ---- Statistiche ----
def today_str() -> str:
    return date.today().isoformat()

def stats_increment(tid: int, sessions=0, tracks=0, minutes=0):
    d = today_str()
    with _db_lock, db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO stats (telegram_id, stat_date) VALUES (?,?)", (tid, d)
        )
        conn.execute("""
            UPDATE stats SET
                sessions       = sessions       + ?,
                tracks_heard   = tracks_heard   + ?,
                mining_minutes = mining_minutes + ?
            WHERE telegram_id=? AND stat_date=?
        """, (sessions, tracks, minutes, tid, d))
        conn.commit()

def stats_get_today(tid: int) -> dict:
    d = today_str()
    with _db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM stats WHERE telegram_id=? AND stat_date=?", (tid, d)
        ).fetchone()
        return dict(row) if row else {"sessions": 0, "tracks_heard": 0, "mining_minutes": 0}

def stats_get_total(tid: int) -> dict:
    with _db_lock, db_connect() as conn:
        row = conn.execute("""
            SELECT
                SUM(sessions)       AS sessions,
                SUM(tracks_heard)   AS tracks_heard,
                SUM(mining_minutes) AS mining_minutes,
                COUNT(DISTINCT stat_date) AS days_active
            FROM stats WHERE telegram_id=?
        """, (tid,)).fetchone()
        if row and row["sessions"] is not None:
            return dict(row)
        return {"sessions": 0, "tracks_heard": 0, "mining_minutes": 0, "days_active": 0}

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
# OAUTH SERVER
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

@_oauth_app.get("/callback")
def oauth_cb():
    err   = request.args.get("error", "")
    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    log.info(f"[OAuth] callback — code={'OK' if code else 'MANCANTE'} state={state!r}")

    if err:
        return f"<h3>Errore Spotify: {err}</h3>Torna su Telegram.", 400
    if not code:
        return "<h3>Codice mancante.</h3>Torna su Telegram.", 400

    tid = _pending.pop(state, None)
    if not tid:
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
        "✅ *Spotify connesso con successo!*\n\n"
        "⛏️ Il mining parte automaticamente mentre ascolti musica.\n"
        "📊 Usa /stats per vedere le tue statistiche.\n\n"
        "Usa /menu per tutti i controlli 👇"
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
    _oauth_app.run("127.0.0.1", CALLBACK_PORT, debug=False, use_reloader=False)

# -------------------------------------------------------
# MINING MONITOR
# -------------------------------------------------------
_session_start: dict = {}   # tid → timestamp inizio sessione corrente

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

    # Niente in play
    if data.get("_204") or not data.get("is_playing"):
        if user.get("last_track"):
            db_set(tid, last_track="")
            # Calcola minuti sessione
            if tid in _session_start:
                mins = max(1, int((now_ts() - _session_start.pop(tid)) / 60))
                stats_increment(tid, minutes=mins)
            _run_async(_send(tid, "⏹️ *Musica ferma* — mining in pausa."))
        return

    # Musica in play
    item     = data.get("item") or {}
    track    = item.get("name", "")
    artist   = ", ".join(a["name"] for a in item.get("artists", []))
    album    = (item.get("album") or {}).get("name", "")
    duration = max(item.get("duration_ms", 1), 1)
    progress = data.get("progress_ms", 0)
    pct      = int(progress / duration * 100)
    bar      = "▓" * (pct // 10) + "░" * (10 - pct // 10)
    label    = f"{artist} — {track}" if track else "brano sconosciuto"

    if label != user.get("last_track", ""):
        db_set(tid, last_track=label)

        # Aggiorna stats
        stats_increment(tid, sessions=1, tracks=1)
        _session_start[tid] = now_ts()

        # Notifica bella con album e progress
        album_line = f"💿 _{album}_\n" if album else ""
        msg = (
            f"🎵 *{track}*\n"
            f"👤 {artist}\n"
            f"{album_line}"
            f"{bar} {pct}%\n\n"
            f"⛏️ Mining avviato!"
        )
        _run_async(_send(tid, msg))

# -------------------------------------------------------
# DAILY SUMMARY — ogni sera alle DAILY_SUMMARY_HOUR
# -------------------------------------------------------
def daily_summary_scheduler():
    """Manda un riassunto giornaliero a tutti gli utenti attivi."""
    log.info("Daily summary scheduler avviato.")
    sent_today = set()

    while True:
        now  = datetime.now()
        hour = now.hour
        day  = now.date().isoformat()

        if hour == DAILY_SUMMARY_HOUR and day not in sent_today:
            sent_today.add(day)
            # Pulisce i giorni vecchi dal set (tiene solo ultimi 2)
            if len(sent_today) > 2:
                sent_today.pop()

            for user in db_all_with_token():
                tid   = user["telegram_id"]
                today = stats_get_today(tid)
                if today["sessions"] == 0:
                    continue  # Non ha minato oggi, non disturbiamo

                name = user.get("first_name") or "utente"
                msg  = (
                    f"🌙 *Riassunto di oggi, {name}!*\n\n"
                    f"⛏️ Sessioni mining: *{today['sessions']}*\n"
                    f"🎵 Brani ascoltati: *{today['tracks_heard']}*\n"
                    f"⏱️ Minuti minati: *{today['mining_minutes']}*\n\n"
                    f"Continua così! 🚀"
                )
                _run_async(_send(tid, msg))
                log.info(f"Daily summary inviato a {tid}")

        time.sleep(60)  # controlla ogni minuto

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
            InlineKeyboardButton("🔄 Stato",      callback_data="status"),
            InlineKeyboardButton("📊 Statistiche", callback_data="stats"),
        ])
        rows.append([
            InlineKeyboardButton("📋 Playlist",   callback_data="playlists"),
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
    tid   = update.effective_user.id
    name  = update.effective_user.first_name or "utente"
    db_set(tid,
           username   = update.effective_user.username or "",
           first_name = name)
    user   = db_get(tid)
    authed = bool(user and user.get("access_token"))

    if authed:
        total = stats_get_total(tid)
        txt = (
            f"👋 Bentornato *{name}*!\n\n"
            f"📊 *Le tue statistiche totali:*\n"
            f"⛏️ Sessioni: {total['sessions']}\n"
            f"🎵 Brani: {total['tracks_heard']}\n"
            f"⏱️ Minuti minati: {total['mining_minutes']}\n\n"
            f"Il bot sta monitorando Spotify in automatico 🚀"
        )
    else:
        txt = (
            f"👋 Ciao *{name}*! Benvenuto su *SpoteeBeeBot* 🐝\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎵 *Listen & Mine — Acki Nacki*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ascolta musica su Spotify e mina token NACKL automaticamente.\n\n"
            "✅ *Come funziona:*\n"
            "1️⃣ Connetti il tuo account Spotify\n"
            "2️⃣ Ascolta musica normalmente\n"
            "3️⃣ Il bot mina in automatico per te\n"
            "4️⃣ Guarda le tue statistiche con /stats\n\n"
            "Premi il bottone qui sotto per iniziare 👇"
        )
    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb(user))

async def h_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = db_get(update.effective_user.id)
    await update.message.reply_text(
        "📱 *Menu SpoteeBeeBot*", parse_mode="Markdown", reply_markup=main_kb(user)
    )

async def h_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid  = update.effective_user.id
    user = db_get(tid)
    if not user or not user.get("access_token"):
        await update.message.reply_text("❌ Devi prima connetterti a Spotify. Usa /start.")
        return
    await _send_stats(tid, update.message, edit=False)

async def _send_stats(tid: int, message, edit=False):
    today = stats_get_today(tid)
    total = stats_get_total(tid)
    user  = db_get(tid)
    name  = (user or {}).get("first_name") or "utente"

    # Calcola ore e minuti
    def fmt_time(mins):
        if mins < 60:
            return f"{mins} min"
        return f"{mins // 60}h {mins % 60}min"

    txt = (
        f"📊 *Statistiche di {name}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 *Oggi ({today_str()}):*\n"
        f"⛏️ Sessioni mining: *{today['sessions']}*\n"
        f"🎵 Brani ascoltati: *{today['tracks_heard']}*\n"
        f"⏱️ Tempo minato: *{fmt_time(today['mining_minutes'])}*\n\n"
        f"🏆 *Totale storico:*\n"
        f"⛏️ Sessioni totali: *{total['sessions']}*\n"
        f"🎵 Brani totali: *{total['tracks_heard']}*\n"
        f"⏱️ Tempo totale: *{fmt_time(total['mining_minutes'])}*\n"
        f"📆 Giorni attivi: *{total['days_active']}*\n"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Aggiorna", callback_data="stats"),
        InlineKeyboardButton("🔙 Menu",     callback_data="back"),
    ]])

    if edit:
        await message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)

# -------------------------------------------------------
# CALLBACK BOTTONI
# -------------------------------------------------------
async def h_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    tid  = update.effective_user.id
    data = q.data
    user = db_get(tid)

    # --- Connetti Spotify ---
    if data == "connect":
        state           = secrets.token_urlsafe(16)
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
                "✅ *Connesso!*\nMining automatico attivo 🚀\n\nUsa /stats per le statistiche.",
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

    elif data == "stats":
        await _send_stats(tid, q.message, edit=True)

    elif data == "mining_on":
        db_set(tid, mining_active=1)
        await q.edit_message_text("▶️ *Mining riattivato!*\nIl bot monitora Spotify in automatico.",
            parse_mode="Markdown", reply_markup=main_kb(db_get(tid)))

    elif data == "mining_off":
        db_set(tid, mining_active=0, last_track="")
        if tid in _session_start:
            mins = max(1, int((now_ts() - _session_start.pop(tid)) / 60))
            stats_increment(tid, minutes=mins)
        await q.edit_message_text("⏸️ *Mining sospeso.*\nPuoi riattivarlo quando vuoi.",
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
        await _edit_playlists(q, user, page=0)

    elif data.startswith("plpage:"):
        page = int(data.split(":")[1])
        await _edit_playlists(q, user, page=page)

    elif data.startswith("pl:"):
        # Mostra i brani della playlist
        pl_id = data.split(":")[1]
        await _edit_playlist_tracks(q, user, pl_id, page=0)

    elif data.startswith("pltracks:"):
        # Paginazione brani
        parts = data.split(":")
        pl_id = parts[1]
        page  = int(parts[2])
        await _edit_playlist_tracks(q, user, pl_id, page=page)

    elif data.startswith("playpl:"):
        # Avvia la playlist
        uri = data.split(":", 1)[1]
        res = sp_put(user, "/me/player/play", body={"context_uri": uri})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Playlist avviata!")
        else:
            await q.answer("⚠️ Serve Spotify Premium e un device attivo.", show_alert=True)

    elif data.startswith("playtrack:"):
        # Avvia brano specifico
        uri = data.split(":", 1)[1]
        res = sp_put(user, "/me/player/play", body={"uris": [uri]})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Brano avviato!")
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
        await q.edit_message_text("📱 *Menu SpoteeBeeBot*", parse_mode="Markdown",
            reply_markup=main_kb(db_get(tid)))

    elif data == "noop":
        pass  # bottone decorativo, non fa nulla

    elif data == "back_playlists":
        await _edit_playlists(q, user, page=0)

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
        txt = f"⏹️ *Niente in riproduzione*\n\n{m}"
    else:
        item    = data.get("item") or {}
        track   = item.get("name", "?")
        artist  = ", ".join(a["name"] for a in item.get("artists", []))
        album   = (item.get("album") or {}).get("name", "")
        prog    = data.get("progress_ms", 0)
        dur     = max(item.get("duration_ms", 1), 1)
        pct     = int(prog / dur * 100)
        bar     = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        m       = "⛏️ Mining attivo" if user.get("mining_active") else "⏸️ Mining sospeso"
        txt     = (
            f"▶️ *{track}*\n"
            f"👤 {artist}\n"
            f"💿 _{album}_\n\n"
            f"{bar} {pct}%\n\n"
            f"{m}"
        )

    await q.edit_message_text(txt, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aggiorna", callback_data="status"),
            InlineKeyboardButton("📊 Stats",    callback_data="stats"),
            InlineKeyboardButton("🔙 Menu",     callback_data="back"),
        ]]))

# -------------------------------------------------------
# Helper: lista playlist con paginazione
# -------------------------------------------------------
async def _edit_playlists(q, user, page=0):
    if not user:
        await q.edit_message_text("❌ Non connesso.")
        return

    limit  = 8
    offset = page * limit
    data   = sp_get(user, "/me/playlists", params={"limit": limit, "offset": offset})
    items  = (data or {}).get("items", [])
    total  = (data or {}).get("total", 0)
    pages  = max(1, (total + limit - 1) // limit)

    back = [[InlineKeyboardButton("🔙 Menu", callback_data="back")]]

    if not items:
        await q.edit_message_text("📋 Nessuna playlist trovata.",
            reply_markup=InlineKeyboardMarkup(back))
        return

    rows = []
    for p in items:
        name  = p.get("name", "?")
        total_tracks = p.get("tracks", {}).get("total", "?")
        pl_id = p.get("id", "")
        # Bottone che mostra i brani della playlist
        rows.append([InlineKeyboardButton(
            f"📋 {name} ({total_tracks} brani)",
            callback_data=f"pl:{pl_id}"
        )])

    # Paginazione
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prec", callback_data=f"plpage:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️ Succ", callback_data=f"plpage:{page+1}"))
    if nav:
        rows.append(nav)
    rows += back

    await q.edit_message_text(
        f"📋 *Le tue playlist* (pag. {page+1}/{pages})\nPremi una playlist per vedere i brani:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )

# -------------------------------------------------------
# Helper: brani di una playlist
# -------------------------------------------------------
async def _edit_playlist_tracks(q, user, pl_id: str, page=0):
    if not user:
        await q.edit_message_text("❌ Non connesso.")
        return

    limit  = 6  # meno brani per pagina = bottoni più leggibili
    offset = page * limit

    # Carica info playlist
    pl_data = sp_get(user, f"/playlists/{pl_id}",
                     params={"fields": "name,uri,tracks.total"})
    pl_name = (pl_data or {}).get("name", "Playlist")
    pl_uri  = (pl_data or {}).get("uri", "")
    total   = (pl_data or {}).get("tracks", {}).get("total", 0)
    pages   = max(1, (total + limit - 1) // limit)

    # Carica brani (senza fields filter per evitare problemi)
    tracks_data = sp_get(user, f"/playlists/{pl_id}/tracks",
                         params={"limit": limit, "offset": offset,
                                 "market": "IT"})

    if not tracks_data or tracks_data.get("_err"):
        await q.edit_message_text(
            "⚠️ Errore nel caricare i brani. Riprova.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")
            ]])
        )
        return

    items = tracks_data.get("items", [])
    rows  = []

    # Bottone avvia playlist intera
    if pl_uri:
        rows.append([InlineKeyboardButton(
            f"▶️ Avvia tutta «{pl_name[:25]}»",
            callback_data=f"playpl:{pl_uri}"
        )])

    # Brani
    for item in items:
        track = item.get("track") if item else None
        if not track or track.get("type") == "episode":
            continue
        name   = track.get("name", "?")[:28]
        artist = (track.get("artists") or [{}])[0].get("name", "")[:18]
        uri    = track.get("uri", "")
        dur_ms = track.get("duration_ms", 0)
        mins   = dur_ms // 60000
        secs   = (dur_ms % 60000) // 1000

        rows.append([InlineKeyboardButton(
            f"▶️ {name} — {artist} ({mins}:{secs:02d})",
            callback_data=f"playtrack:{uri}"
        )])

    if not rows or len(rows) <= 1:
        rows.append([InlineKeyboardButton("(Nessun brano trovato)", callback_data="noop")])

    # Navigazione pagine
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pltracks:{pl_id}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pltracks:{pl_id}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")])

    await q.edit_message_text(
        f"📋 *{pl_name}*\n_{total} brani totali_\n\nPremi ▶️ per avviare un brano:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )

# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
def main():
    global _tg_app

    db_init()

    # Server OAuth in background
    threading.Thread(target=_start_oauth_server, daemon=True).start()
    log.info(f"Server OAuth su http://127.0.0.1:{CALLBACK_PORT}")

    # Mining monitor in background
    threading.Thread(target=mining_monitor, daemon=True).start()

    # Daily summary scheduler in background
    threading.Thread(target=daily_summary_scheduler, daemon=True).start()
    log.info(f"Daily summary attivo — invio alle {DAILY_SUMMARY_HOUR}:00")

    # Bot Telegram
    _tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    _tg_app.add_handler(CommandHandler("start", h_start))
    _tg_app.add_handler(CommandHandler("menu",  h_menu))
    _tg_app.add_handler(CommandHandler("stats", h_stats))
    _tg_app.add_handler(CallbackQueryHandler(h_button))

    print("\n" + "="*50)
    print("  SPOTEEBEEBOT v2.0 AVVIATO ✅")
    print(f"  Cerca @SpoteeBeeBot su Telegram → /start")
    print(f"  Daily summary ogni giorno alle {DAILY_SUMMARY_HOUR}:00")
    print("="*50 + "\n")

    _tg_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
