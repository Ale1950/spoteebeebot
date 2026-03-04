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
# CONFIG — funziona sia in locale (config.json) che su Railway (env vars)
# -------------------------------------------------------
CONFIG_FILE = "config.json"

def load_config() -> dict:
    # Railway: legge da variabili d'ambiente
    if os.environ.get("TELEGRAM_TOKEN"):
        log.info("Config da variabili d'ambiente (Railway)")
        return {
            "TELEGRAM_TOKEN":        os.environ["TELEGRAM_TOKEN"],
            "SPOTIFY_CLIENT_ID":     os.environ.get("SPOTIFY_CLIENT_ID", "21675318528e48c9a7a5c85b1f53da54"),
            "SPOTIFY_CLIENT_SECRET": os.environ.get("SPOTIFY_CLIENT_SECRET", "b582f0666fe9434ebd54f691a0d2d3b4"),
            "OAUTH_CALLBACK_PORT":   int(os.environ.get("PORT", 8082)),
            "POLL_INTERVAL_SEC":     int(os.environ.get("POLL_INTERVAL_SEC", 5)),
            "DAILY_SUMMARY_HOUR":    int(os.environ.get("DAILY_SUMMARY_HOUR", 21)),
            "PUBLIC_URL":            os.environ.get("PUBLIC_URL", ""),
        }

    # Locale: legge o crea config.json
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
            "PUBLIC_URL":            "",
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
PUBLIC_URL           = CFG.get("PUBLIC_URL", "")

# Redirect URI: usa URL pubblico su Railway, locale in sviluppo
if PUBLIC_URL:
    REDIRECT_URI = f"{PUBLIC_URL}/callback"
else:
    REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"

log.info(f"REDIRECT_URI = {REDIRECT_URI}")
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL= "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
# Scope base — non richiedono Premium, funzionano con account Free
SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-modify-playback-state"
)
# Nota: user-modify-playback-state richiede Premium su Spotify
# ma NON blocca il login — Spotify lo accetta anche su Free
# (i comandi play/pause daranno 403 su Free, ma il login funziona)

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
_main_loop     = None  # event loop del bot, usato per notifiche cross-thread

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

    # Verifica subito se l'account è Premium
    user_data   = db_get(tid)
    profile     = None
    premium_ok  = False
    if user_data:
        profile    = sp_get(user_data, "/me")
        product    = (profile or {}).get("product", "")
        premium_ok = product == "premium"
        sp_name    = (profile or {}).get("display_name", "")
        log.info(f"[OAuth] Account: {sp_name} / product={product}")

    if premium_ok:
        confirm_msg = (
            f"`{SEP}`\n"
            f"✅ *SPOTIFY PREMIUM CONNESSO!*\n"
            f"`{SEP}`\n\n"
            f"👤 Account: *{sp_name}*\n"
            f"⭐ Piano: *Premium* ✅\n\n"
            "`▸ ACKI NACKI è pronto!`\n"
            "⛏️ Mine Nackles parte automaticamente\n"
            "non appena ascolti musica 🎵\n\n"
            "• /stats — le tue statistiche\n"
            "• /menu — tutti i controlli"
        )
    else:
        product_label = (profile or {}).get("product", "sconosciuto")
        confirm_msg = (
            f"`{SEP}`\n"
            f"⚠️ *ATTENZIONE — ACCOUNT NON PREMIUM*\n"
            f"`{SEP}`\n\n"
            f"Piano rilevato: *{product_label}*\n\n"
            "I comandi play/pause/next richiedono\n"
            "*Spotify Premium*. Hai connesso l'account\n"
            "sbagliato?\n\n"
            "🔴 Premi *Disconnetti* nel menu,\n"
            "poi riconnetti con l'account Premium."
        )

    threading.Thread(target=_async_notify, args=(tid, confirm_msg), daemon=True).start()

    return """<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Acki Nacki — Connesso!</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      background: #0e0b08;
      color: #e8a87c;
      font-family: -apple-system, sans-serif;
      min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
    }
    .card {
      background: #1a0d08;
      border: 1px solid #c0392b55;
      border-radius: 20px;
      padding: 40px 30px;
      max-width: 400px;
      width: 90%;
      text-align: center;
    }
    .icon { font-size: 60px; margin-bottom: 20px; }
    h2 { font-size: 22px; color: #e8a87c; margin-bottom: 10px; letter-spacing: 1px; }
    .sep { color: #c0392b; font-size: 20px; margin: 15px 0; letter-spacing: 4px; }
    p { font-size: 15px; color: #7a5a3a; line-height: 1.6; margin-bottom: 20px; }
    .btn {
      display: inline-block;
      background: linear-gradient(135deg, #c0392b, #922b21);
      color: white;
      text-decoration: none;
      padding: 14px 30px;
      border-radius: 50px;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 1px;
      margin: 8px;
    }
    .btn-tg { background: linear-gradient(135deg, #2481cc, #1a6aaa); }
    .sig { font-size: 11px; color: #3a1a0a; margin-top: 25px; font-style: italic; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h2>SPOTIFY CONNESSO!</h2>
    <div class="sep">▬▬▬▬▬▬▬▬</div>
    <p>⛏️ Mine Nackles è pronto.<br>Torna su Telegram per iniziare.</p>
    <a href="tg://resolve?domain=SpoteeBeeBot" class="btn btn-tg">📱 Apri Telegram</a>
    <p class="sig">— Acki Jewels 💎</p>
  </div>
  <script>
    // Auto-redirect a Telegram dopo 2 secondi
    setTimeout(function() {
      window.location.href = "tg://resolve?domain=SpoteeBeeBot";
    }, 2000);
  </script>
</body>
</html>"""

def _async_notify(tid: int, text: str):
    time.sleep(1)
    _run_async(_send(tid, text))

def _run_async(coro):
    global _main_loop
    try:
        if _main_loop and _main_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, _main_loop)
        else:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(coro)
            loop.close()
    except Exception as e:
        log.error(f"Async error: {e}")

def _start_oauth_server():
    # Su Railway: ascolta su 0.0.0.0 con la porta assegnata da Railway
    # In locale: ascolta su 127.0.0.1:8082
    host = "0.0.0.0" if PUBLIC_URL else "127.0.0.1"
    port = int(os.environ.get("PORT", CALLBACK_PORT))
    log.info(f"Flask OAuth server su {host}:{port}")
    _oauth_app.run(host, port, debug=False, use_reloader=False)

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

    if data.get("_204") or not data.get("is_playing"):
        if user.get("last_track"):
            db_set(tid, last_track="")
            if tid in _session_start:
                mins = max(1, int((now_ts() - _session_start.pop(tid)) / 60))
                stats_increment(tid, minutes=mins)
            _run_async(_send(tid,
                f"`{SEP_S}`\n"
                "⏹ *Musica ferma*\n"
                "⚫ Mine Nackles in pausa\n"
                f"`{SEP_S}`"
            ))
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
        stats_increment(tid, sessions=1, tracks=1)
        _session_start[tid] = now_ts()

        album_line = f"💿 _{album}_\n" if album else ""
        msg = (
            f"{hdr_track()}\n"
            f"🎵 *{track}*\n"
            f"🔴 {artist}\n"
            f"{album_line}"
            f"`▸ NUOVO BRANO`\n"
            f"{hdr_track()}\n\n"
            f"⛏️ *Mine Nackles AVVIATO!*"
            f"{firma()}"
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
                    f"`{SEP}`\n"
                    f"🌙 *ACKI NACKI · REPORT SERALE*\n"
                    f"`{SEP}`\n\n"
                    f"👤 *{name}* — ottimo oggi! 🔥\n\n"
                    f"`▸ MINE NACKLES OGGI`\n"
                    f"⛏️  Sessioni:  *{today['sessions']}*\n"
                    f"🎵  Brani:     *{today['tracks_heard']}*\n"
                    f"⏱️  Minuti:    *{today['mining_minutes']}*\n\n"
                    f"🔴 _Continua domani!_"
                    f"{firma()}"
                )
                _run_async(_send(tid, msg))
                log.info(f"Daily summary inviato a {tid}")

        time.sleep(60)  # controlla ogni minuto

# -------------------------------------------------------
# VISUAL DESIGN — testi e decorazioni
# -------------------------------------------------------
ACKI_IMAGE = "acki_music.png"  # immagine locale

# ─────────────────────────────────────────────────────────
# OBSIDIAN EMBER DESIGN SYSTEM
# Dark premium: carbone + cremisi + ambra  (Acki Jewels)
# ─────────────────────────────────────────────────────────
SEP   = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
SEP_S = "· · · · · · · · ·"

def firma() -> str:
    return "\n`                — Acki Jewels 💎`"

def hdr_main() -> str:
    return (
        f"`{SEP}`\n"
        "    🐝  *A C K I   N A C K I*\n"
        "    ⛏️  *L I S T E N  &  M I N E*\n"
        f"`{SEP}`"
    )

def hdr_menu() -> str:
    return (
        f"`{SEP}`\n"
        "  🔥  *ACKI NACKI · MENU*  🔥\n"
        f"`{SEP}`"
    )

def hdr_stats() -> str:
    return (
        f"`{SEP}`\n"
        "  📊  *MINE NACKLES · STATS*\n"
        f"`{SEP}`"
    )

def hdr_playlist() -> str:
    return (
        f"`{SEP}`\n"
        "  🎼  *LE TUE PLAYLIST*\n"
        f"`{SEP}`"
    )

def hdr_track() -> str:
    return f"`{SEP_S}`"

def mining_status_line(active: bool) -> str:
    if active:
        return "🔴 *Mine Nackles:* `● ATTIVO`  ⛏️"
    return "⚫ *Mine Nackles:* `○ IN PAUSA`"

def progress_bar(pct: int) -> str:
    """Barra stile Obsidian Ember: blocchi pieni + vuoti."""
    filled = pct // 10
    return "🟥" * filled + "⬛" * (10 - filled)

SPOTIFY_OPEN_URL = "https://open.spotify.com"  # fallback web
SPOTIFY_APP_URL  = "https://open.spotify.com"  # deep link web (spotify:// non supportato da Telegram)

# -------------------------------------------------------
# TELEGRAM — send helpers
# -------------------------------------------------------
async def _send(tid: int, text: str, markup=None):
    if not _tg_app:
        return
    try:
        await _tg_app.bot.send_message(
            chat_id=tid, text=text,
            reply_markup=markup
        )
    except Exception as e:
        log.error(f"Send error a {tid}: {e}")

async def _edit(q, txt: str, markup=None):
    """Modifica il messaggio sia se è foto (caption) che testo normale."""
    try:
        await q.edit_message_caption(caption=txt, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        try:
            await q.edit_message_text(text=txt, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            log.error(f"Edit error: {e}")

async def _send_photo(tid: int, caption: str, markup=None):
    """Manda il messaggio con l'immagine Acki Jewels come header."""
    if not _tg_app:
        return
    try:
        if os.path.exists(ACKI_IMAGE):
            with open(ACKI_IMAGE, "rb") as img:
                await _tg_app.bot.send_photo(
                    chat_id=tid,
                    photo=img,
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
        else:
            await _send(tid, caption, markup)
    except Exception as e:
        log.error(f"Send photo error a {tid}: {e}")
        await _send(tid, caption, markup)

# -------------------------------------------------------
# KEYBOARDS — con gemme e brillanti
# -------------------------------------------------------
def main_kb(user: dict | None) -> InlineKeyboardMarkup:
    authed = bool(user and user.get("access_token"))
    mining = bool(user and user.get("mining_active"))
    rows   = []
    if not authed:
        rows.append([InlineKeyboardButton("🎵 Connetti Spotify", callback_data="connect")])
    else:
        rows.append([
            InlineKeyboardButton("🔴 Stato",   callback_data="status"),
            InlineKeyboardButton("📊 Stats",   callback_data="stats"),
            InlineKeyboardButton("🎼 Playlist", callback_data="playlists"),
        ])
        rows.append([InlineKeyboardButton(
            "⏸ Sospendi Mine Nackles" if mining else "⛏️ Avvia Mine Nackles",
            callback_data="mining_off" if mining else "mining_on"
        )])
        # Controlli riproduzione — riga grande
        rows.append([
            InlineKeyboardButton("⏮",  callback_data="prev"),
            InlineKeyboardButton("▶",  callback_data="play"),
            InlineKeyboardButton("⏸",  callback_data="pause"),
            InlineKeyboardButton("⏭",  callback_data="next"),
        ])
        # Shuffle + Repeat
        shuffle_on = bool(user and user.get("shuffle_on"))
        repeat_mode = (user or {}).get("repeat_mode", "off")  # off / context / track
        rows.append([
            InlineKeyboardButton(
                "🔀 Shuffle ON" if shuffle_on else "🔀 Shuffle",
                callback_data="shuffle_toggle"
            ),
            InlineKeyboardButton(
                "🔂 1 brano" if repeat_mode == "track" else
                ("🔁 Playlist" if repeat_mode == "context" else "🔁 Repeat"),
                callback_data="repeat_toggle"
            ),
        ])
        # Apri Spotify + Disconnetti
        rows.append([
            InlineKeyboardButton("🎧 Apri Spotify", url=SPOTIFY_APP_URL),
            InlineKeyboardButton("🔌 Disconnetti",  callback_data="disconnect"),
        ])
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
        total  = stats_get_total(tid)
        mining = bool(user and user.get("mining_active"))

        def fmt(m): return f"{m//60}h {m%60}min" if m >= 60 else f"{m} min"

        txt = (
            f"{hdr_main()}\n\n"
            f"👋 Bentornato *{name}*!\n\n"
            f"`▸ MINE NACKLES`\n"
            f"⛏️  Sessioni:    *{total['sessions']}*\n"
            f"🎵  Brani:       *{total['tracks_heard']}*\n"
            f"⏱️  Tempo totale: *{fmt(total['mining_minutes'])}*\n"
            f"📆  Giorni attivi: *{total['days_active']}*\n\n"
            f"{mining_status_line(mining)}"
            f"{firma()}"
        )
    else:
        txt = (
            f"{hdr_main()}\n\n"
            f"👋 Ciao *{name}*! Benvenuto!\n\n"
            f"`▸ COME FUNZIONA`\n"
            f"1️⃣  Connetti Spotify\n"
            f"2️⃣  Ascolta musica normalmente\n"
            f"3️⃣  ⛏️ Mine Nackles parte in automatico\n"
            f"4️⃣  Controlla stats con /stats\n\n"
            f"🔴 _Premi il bottone qui sotto_"
            f"{firma()}"
        )

    if os.path.exists(ACKI_IMAGE):
        with open(ACKI_IMAGE, "rb") as img:
            await update.message.reply_photo(
                photo=img,
                caption=txt,
                parse_mode="Markdown",
                reply_markup=main_kb(user)
            )
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb(user))

async def h_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = db_get(update.effective_user.id)
    mining = bool(user and user.get("mining_active"))
    txt = (
        f"{hdr_menu()}\n\n"
        f"{mining_status_line(mining)}\n"
        f"`▸ scegli un'opzione`"
    )
    if os.path.exists(ACKI_IMAGE):
        with open(ACKI_IMAGE, "rb") as img:
            await update.message.reply_photo(
                photo=img,
                caption=txt,
                parse_mode="Markdown",
                reply_markup=main_kb(user)
            )
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb(user))

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

    def fmt(m): return f"{m//60}h {m%60}min" if m >= 60 else f"{m} min"

    # Barra progresso brani oggi (max 10)
    t   = min(today["tracks_heard"], 10)
    bar = "🟥" * t + "⬛" * (10 - t)

    txt = (
        f"{hdr_stats()}\n\n"
        f"👤 *{name}*\n\n"
        f"`▸ OGGI — {today_str()}`\n"
        f"⛏️  Sessioni:  *{today['sessions']}*\n"
        f"🎵  Brani:     *{today['tracks_heard']}*\n"
        f"⏱️  Tempo:     *{fmt(today['mining_minutes'])}*\n"
        f"{bar}\n\n"
        f"`▸ TOTALE STORICO`\n"
        f"🔴  Sessioni:  *{total['sessions']}*\n"
        f"🎶  Brani:     *{total['tracks_heard']}*\n"
        f"⏱️  Totale:    *{fmt(total['mining_minutes'])}*\n"
        f"📆  Giorni:    *{total['days_active']}*\n"
        f"{firma()}"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Aggiorna", callback_data="stats"),
        InlineKeyboardButton("🔙 Menu",     callback_data="back"),
    ]])

    if edit:
        await message.edit_text(txt, kb)
    else:
        await message.reply_text(txt, kb)

# -------------------------------------------------------
# Helper: azioni player con controllo device
# -------------------------------------------------------
async def _player_action(q, user: dict, action: str):
    """Esegue un'azione player. Se nessun device attivo, manda link per aprire Spotify."""
    # Controlla device disponibili
    devices_data = sp_get(user, "/me/player/devices")
    devices = (devices_data or {}).get("devices", [])
    active  = [d for d in devices if d.get("is_active")]

    if not devices:
        # Nessun device trovato — Spotify non è aperto da nessuna parte
        await _edit(q, 
            "📱 *Spotify non è aperto su nessun dispositivo.*\n\n"
            "Apri Spotify sul tuo telefono o PC, poi torna qui e riprova.\n\n"
            "👉 [Apri Spotify](https://open.spotify.com)",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Riprova", callback_data=f"{action}_retry"),
                InlineKeyboardButton("🔙 Menu",    callback_data="back"),
            ]])
        )
        return

    # Usa il device attivo o il primo disponibile
    device_id = (active[0] if active else devices[0])["id"]

    if action == "play":
        res = sp_put(user, "/me/player/play", params={"device_id": device_id})
    elif action == "pause":
        res = sp_put(user, "/me/player/pause", params={"device_id": device_id})
    elif action == "next":
        res = sp_post(user, "/me/player/next")
        res = res or {}
    elif action == "prev":
        res = sp_post(user, "/me/player/previous")
        res = res or {}
    else:
        return

    status = (res or {}).get("_status", 0)

    if status in (200, 202, 204):
        icons = {"play": "▶️ Avviata!", "pause": "⏸️ Pausa.", "next": "⏭️ Avanti!", "prev": "⏮️ Indietro!"}
        await q.answer(icons.get(action, "✅"), show_alert=False)
    elif status == 403:
        await q.answer("⚠️ Serve Spotify Premium per questo comando.", show_alert=True)
    elif status == 404:
        await _edit(q, 
            "📱 *Nessun dispositivo attivo trovato.*\n\n"
            "Apri Spotify e avvia un brano, poi riprova.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu", callback_data="back")
            ]])
        )
    else:
        await q.answer(f"⚠️ Errore ({status}). Riprova.", show_alert=True)


async def _toggle_shuffle(q, user: dict):
    """Attiva/disattiva shuffle su Spotify e aggiorna DB."""
    tid       = user["telegram_id"]
    current   = bool(user.get("shuffle_on"))
    new_state = not current
    state_str = "true" if new_state else "false"

    devices_data = sp_get(user, "/me/player/devices")
    devices = [(d or {}) for d in (devices_data or {}).get("devices", [])]
    active  = [d for d in devices if d.get("is_active")]
    if not devices:
        await q.answer("⚠️ Apri Spotify prima di usare shuffle.", show_alert=True)
        return

    device_id = (active[0] if active else devices[0])["id"]
    res    = sp_put(user, "/me/player/shuffle",
                    params={"state": state_str, "device_id": device_id})
    status = (res or {}).get("_status", 0)

    if status in (200, 202, 204):
        db_set(tid, shuffle_on=1 if new_state else 0)
        label = "🔀 Shuffle ON ✅" if new_state else "🔀 Shuffle OFF"
        await q.answer(label, show_alert=False)
        # Aggiorna tastiera
        updated_user = db_get(tid)
        await _edit(q,
            f"`{SEP}`\n"
            f"{'🔀 *Shuffle attivato!*' if new_state else '🔀 *Shuffle disattivato*'}\n"
            f"`{SEP}`",
            markup=main_kb(updated_user)
        )
    elif status == 403:
        await q.answer("⚠️ Serve Spotify Premium.", show_alert=True)
    else:
        await q.answer(f"⚠️ Errore ({status})", show_alert=True)


async def _toggle_repeat(q, user: dict):
    """Cicla modalità repeat: off → context → track → off."""
    tid         = user["telegram_id"]
    current     = (user or {}).get("repeat_mode", "off")
    cycle       = {"off": "context", "context": "track", "track": "off"}
    new_mode    = cycle.get(current, "off")
    labels      = {"off": "🔁 Repeat OFF", "context": "🔁 Playlist ON ✅", "track": "🔂 1 Brano ON ✅"}

    devices_data = sp_get(user, "/me/player/devices")
    devices = [(d or {}) for d in (devices_data or {}).get("devices", [])]
    active  = [d for d in devices if d.get("is_active")]
    if not devices:
        await q.answer("⚠️ Apri Spotify prima di usare repeat.", show_alert=True)
        return

    device_id = (active[0] if active else devices[0])["id"]
    res    = sp_put(user, "/me/player/repeat",
                    params={"state": new_mode, "device_id": device_id})
    status = (res or {}).get("_status", 0)

    if status in (200, 202, 204):
        db_set(tid, repeat_mode=new_mode)
        await q.answer(labels.get(new_mode, "✅"), show_alert=False)
        updated_user = db_get(tid)
        await _edit(q,
            f"`{SEP}`\n"
            f"{labels.get(new_mode, '🔁 Repeat')}\n"
            f"`{SEP}`",
            markup=main_kb(updated_user)
        )
    elif status == 403:
        await q.answer("⚠️ Serve Spotify Premium.", show_alert=True)
    else:
        await q.answer(f"⚠️ Errore ({status})", show_alert=True)


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
            "state": state,
            # show_dialog: NON forzare re-login — usa sessione già attiva
            # così chi ha Premium nel browser viene riconosciuto direttamente
        }
        url = SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(params)
        txt = (
            f"`{SEP}`\n"
            "🔐 *CONNETTI SPOTIFY PREMIUM*\n"
            f"`{SEP}`\n\n"
            "`▸ IMPORTANTE — leggi prima`\n"
            "🔴 Apri *l'app Spotify* sul telefono\n"
            "🔴 Assicurati di essere loggato con\n"
            "   il tuo account *Premium*\n\n"
            "`▸ POI SEGUI QUESTI PASSI`\n"
            "1️⃣  Premi il link qui sotto\n"
            "2️⃣  Se Spotify chiede il login →\n"
            "   usa l'email del tuo *Premium*\n"
            "3️⃣  Premi *Accetta*\n"
            "4️⃣  Torna qui e premi il bottone\n\n"
            f"👉 [🎧 Autorizza Spotify Premium]({url})\n\n"
            "⚠️ _Se vedi pagina upgrade: premi Indietro,_\n"
            "_apri Spotify, fai logout e riloggati con Premium_"
            f"{firma()}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💎 Ho autorizzato, controlla!", callback_data="check_auth")
        ]])
        try:
            await _edit(q, txt, kb)
        except Exception:
            await _edit(q, markup=kb)

    elif data == "check_auth":
        user = db_get(tid)
        if user and user.get("access_token"):
            await _edit(q, 
                "✅ *Connesso!*\nMining automatico attivo 🚀\n\nUsa /stats per le statistiche.",
                markup=main_kb(user)
            )
        else:
            await _edit(q, 
                "⏳ Non ho ancora ricevuto l'autorizzazione.\n"
                "Assicurati di aver premuto *Accetta* su Spotify.",
                markup=InlineKeyboardMarkup([[
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
        await _edit(q, "▶️ *Mining riattivato!*\nIl bot monitora Spotify in automatico.",
            markup=main_kb(db_get(tid)))

    elif data == "mining_off":
        db_set(tid, mining_active=0, last_track="")
        if tid in _session_start:
            mins = max(1, int((now_ts() - _session_start.pop(tid)) / 60))
            stats_increment(tid, minutes=mins)
        await _edit(q, "⏸️ *Mining sospeso.*\nPuoi riattivarlo quando vuoi.",
            markup=main_kb(db_get(tid)))

    elif data == "prev":
        await _player_action(q, user, "prev")
    elif data == "shuffle_toggle":
        await _toggle_shuffle(q, user)
    elif data == "repeat_toggle":
        await _toggle_repeat(q, user)
    elif data == "play":
        await _player_action(q, user, "play")
    elif data == "pause":
        await _player_action(q, user, "pause")
    elif data == "next":
        await _player_action(q, user, "next")

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
        uri = data.split(":", 1)[1]
        parts = uri.split(":")
        spotify_url = f"https://open.spotify.com/{parts[1]}/{parts[2]}" if len(parts) == 3 else "https://open.spotify.com"

        devices_data = sp_get(user, "/me/player/devices")
        devices = (devices_data or {}).get("devices", [])
        active  = [d for d in devices if d.get("is_active")]

        if not devices:
            await _edit(q, 
                "📱 *Spotify non è aperto.*\n\n"
                "Apri Spotify prima di avviare una playlist.\n\n"
                f"👉 [Apri Spotify]({spotify_url})",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")
                ]])
            )
            return

        device_id = (active[0] if active else devices[0])["id"]
        res = sp_put(user, "/me/player/play",
                     params={"device_id": device_id},
                     body={"context_uri": uri})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Playlist avviata!")
        else:
            await _edit(q, 
                f"⚠️ Non riesco ad avviare la playlist.\n\n"
                f"Prova ad aprirla direttamente: [Apri in Spotify]({spotify_url})",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")
                ]])
            )

    elif data.startswith("playtrack:"):
        uri = data.split(":", 1)[1]
        parts = uri.split(":")
        spotify_url = f"https://open.spotify.com/{parts[1]}/{parts[2]}" if len(parts) == 3 else "https://open.spotify.com"

        devices_data = sp_get(user, "/me/player/devices")
        devices = (devices_data or {}).get("devices", [])
        active  = [d for d in devices if d.get("is_active")]

        if not devices:
            await _edit(q, 
                "📱 *Spotify non è aperto.*\n\n"
                f"Apri Spotify prima di avviare un brano.\n\n"
                f"👉 [Apri in Spotify]({spotify_url})",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")
                ]])
            )
            return

        device_id = (active[0] if active else devices[0])["id"]
        res = sp_put(user, "/me/player/play",
                     params={"device_id": device_id},
                     body={"uris": [uri]})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Brano avviato!")
        else:
            await _edit(q, 
                f"⚠️ Non riesco ad avviare il brano.\n\n"
                f"Prova ad aprirlo direttamente: [Apri in Spotify]({spotify_url})",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")
                ]])
            )

    elif data == "disconnect":
        db_set(tid, access_token=None, refresh_token=None,
               mining_active=0, last_track="")
        await _edit(q, 
            "🔌 *Disconnesso da Spotify.*\nUsa /start per riconnetterti.",
            parse_mode="Markdown"
        )

    elif data == "back":
        await _edit(q,
            markup=main_kb(db_get(tid)))

    elif data == "noop":
        pass  # bottone decorativo, non fa nulla

    elif data == "back_playlists":
        await _edit_playlists(q, user, page=0)

# -------------------------------------------------------
# Helper: stato corrente
# -------------------------------------------------------
async def _edit_status(q, user):
    if not user or not user.get("access_token"):
        await _edit(q, "❌ Non connesso. Usa /start.")
        return

    data = sp_get(user, "/me/player/currently-playing",
                  params={"additional_types": "track,episode"})

    if data is None:
        txt = "⚠️ Token scaduto. Usa /start."
    elif data.get("_204") or not data.get("is_playing"):
        m   = "⛏️ Mine Nackles pronto — aspetta musica" if user.get("mining_active") else "⚫ Mine Nackles in pausa"
        txt = (
            f"`{SEP_S}`\n"
            f"⏹ *Nessuna riproduzione*\n"
            f"`{SEP_S}`\n\n"
            f"{m}"
            f"{firma()}"
        )
    else:
        item    = data.get("item") or {}
        track   = item.get("name", "?")
        artist  = ", ".join(a["name"] for a in item.get("artists", []))
        album   = (item.get("album") or {}).get("name", "")
        prog    = data.get("progress_ms", 0)
        dur     = max(item.get("duration_ms", 1), 1)
        pct     = int(prog / dur * 100)
        bar     = progress_bar(pct)
        m       = "⛏️ Mine Nackles `ATTIVO`" if user.get("mining_active") else "⚫ Mine Nackles `PAUSA`"
        txt     = (
            f"{hdr_track()}\n"
            f"▶ *{track}*\n"
            f"🔴 {artist}\n"
            f"💿 _{album}_\n\n"
            f"{bar} `{pct}%`\n"
            f"{hdr_track()}\n\n"
            f"{m}"
            f"{firma()}"
        )

    await _edit(q,
        markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aggiorna", callback_data="status"),
            InlineKeyboardButton("📊 Stats",    callback_data="stats"),
            InlineKeyboardButton("🔙 Menu",     callback_data="back"),
        ]]))

# -------------------------------------------------------
# Helper: lista playlist con paginazione
# -------------------------------------------------------
async def _edit_playlists(q, user, page=0):
    if not user:
        await _edit(q, "❌ Non connesso.")
        return

    limit  = 8
    offset = page * limit
    data   = sp_get(user, "/me/playlists", params={"limit": limit, "offset": offset})
    items  = (data or {}).get("items", [])
    total  = (data or {}).get("total", 0)
    pages  = max(1, (total + limit - 1) // limit)

    back = [[InlineKeyboardButton("🔙 Menu", callback_data="back")]]

    if not items:
        await _edit(q, "📋 Nessuna playlist trovata.",
            markup=InlineKeyboardMarkup(back))
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

    await _edit(q, 
        f"{hdr_playlist()}\n\n"
        f"_Pag. {page+1}/{pages} — {total} playlist_\n\n"
        f"Premi una playlist per vedere i brani:",
        markup=InlineKeyboardMarkup(rows)
    )

# -------------------------------------------------------
# Helper: brani di una playlist
# -------------------------------------------------------
async def _edit_playlist_tracks(q, user, pl_id: str, page=0):
    if not user:
        await _edit(q, "❌ Non connesso.")
        return

    limit  = 6  # meno brani per pagina = bottoni più leggibili
    offset = page * limit

    # Carica brani e info playlist in un'unica chiamata
    tracks_data = sp_get(user, f"/playlists/{pl_id}/tracks",
                         params={"limit": limit, "offset": offset})

    if not tracks_data or tracks_data.get("_err") or tracks_data.get("_204"):
        err_code = (tracks_data or {}).get("_err", "?")
        log.error(f"Playlist tracks error: pl_id={pl_id} err={err_code} data={tracks_data}")
        if str(err_code) == "403":
            msg = (
                f"`{SEP}`\n"
                f"🔒 *Playlist non accessibile*\n"
                f"`{SEP}`\n\n"
                f"`▸ CAUSE POSSIBILI`\n"
                "• Playlist di Spotify (Discover Weekly ecc)\n"
                "• Token scaduto o scope mancante\n\n"
                "🔴 _Disconnetti e riconnetti Spotify_\n"
                "_per aggiornare i permessi_"
            )
        else:
            msg = (
                f"⚠️ Errore `{err_code}` nel caricare i brani.\n"
                "Riprova tra qualche secondo."
            )
        await _edit(q, msg,
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Riprova",    callback_data=f"pl:{pl_id}"),
                InlineKeyboardButton("🔙 Playlist",   callback_data="back_playlists"),
            ],[
                InlineKeyboardButton("🔌 Riconnetti", callback_data="disconnect"),
            ]])
        )
        return

    total  = tracks_data.get("total", 0)
    pages  = max(1, (total + limit - 1) // limit)

    # Prendi nome e uri dalla playlist (senza fields filter per compatibilità)
    pl_data = sp_get(user, f"/playlists/{pl_id}")
    pl_name = (pl_data or {}).get("name", "Playlist")
    pl_uri  = (pl_data or {}).get("uri", "")
    log.info(f"Playlist info: name={pl_name} uri={pl_uri} err={( pl_data or {}).get('_err')}")

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

    await _edit(q, 
        f"📋 *{pl_name}*\n_{total} brani totali_\n\nPremi ▶️ per avviare un brano:",
        markup=InlineKeyboardMarkup(rows)
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
    # Store event loop reference for cross-thread use
    _tg_app.add_handler(CommandHandler("start", h_start))
    _tg_app.add_handler(CommandHandler("menu",  h_menu))
    _tg_app.add_handler(CommandHandler("stats", h_stats))
    _tg_app.add_handler(CallbackQueryHandler(h_button))

    print("\n" + "="*50)
    print("  SPOTEEBEEBOT v2.0 AVVIATO ✅")
    print(f"  Cerca @SpoteeBeeBot su Telegram → /start")
    print(f"  Daily summary ogni giorno alle {DAILY_SUMMARY_HOUR}:00")
    print("="*50 + "\n")

    async def _set_loop(app):
        global _main_loop
        _main_loop = asyncio.get_event_loop()
    
    _tg_app.post_init = _set_loop
    _tg_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
