"""
telegram_bot.py v3.0 — Bot Telegram "Listen & Mine"
----------------------------------------------------
Avvio:  python telegram_bot.py
Dipendenze: pip install python-telegram-bot flask requests

Novità v3.0:
- ZERO messaggi extra: shuffle/repeat/now_playing aggiornano solo bottoni e caption
- Repeat/Shuffle sincronizzati con stato REALE di Spotify
- Premium check corretto (aggiunto scope user-read-private)
- Playlist tracks via /playlists/{id} (bypassa 403 di /tracks in Dev Mode)
- Apri Spotify con universal link (apre app se installata)
- Auto-refresh titolo brano nella caption menu foto
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
# Scope completi — playlist-read-private obbligatorio per leggere brani
SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-read-private "
    "user-read-email "
    "user-read-recently-played "
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-library-read "
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

        # ── Migrazioni: aggiunge colonne mancanti se non esistono ──
        existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        migrations = {
            "now_playing_msg_id": "INTEGER DEFAULT 0",
            "shuffle_on":         "INTEGER DEFAULT 0",
            "repeat_mode":        "TEXT DEFAULT 'off'",
            "menu_msg_id":        "INTEGER DEFAULT 0",
        }
        for col, typedef in migrations.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                log.info(f"Colonna '{col}' aggiunta alla tabella users.")
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
        log.error(f"Refresh token failed: {r.status_code} {r.text}")
        return False
    d = r.json()
    db_set(user["telegram_id"],
           access_token  = d["access_token"],
           refresh_token = d.get("refresh_token", rt),
           expires_in    = d.get("expires_in", 3600),
           token_at      = now_ts())
    log.info(f"Token refreshed for {user['telegram_id']}, scopes: {d.get('scope','?')}")
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
        log.warning(f"sp_get {path}: no valid token")
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
        try:
            body = r.json()
        except Exception:
            body = r.text
        log.error(f"sp_get {path} → {r.status_code}: {body}")
        return {"_err": r.status_code, "_body": body}
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
    sp_name     = ""
    product     = "sconosciuto"
    if user_data:
        profile = sp_get(user_data, "/me")
        log.info(f"[OAuth] /me response: {profile}")
        if profile and not profile.get("_err"):
            product    = (profile.get("product") or "").lower()
            premium_ok = product == "premium"
            sp_name    = profile.get("display_name") or profile.get("id") or ""
            log.info(f"[OAuth] Account: {sp_name} / product='{product}' / premium={premium_ok}")
        else:
            log.error(f"[OAuth] /me failed: {profile}")

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
            "Usa /menu per tutti i controlli"
        )
    else:
        confirm_msg = (
            f"`{SEP}`\n"
            f"⚠️ *ATTENZIONE — ACCOUNT NON PREMIUM*\n"
            f"`{SEP}`\n\n"
            f"👤 Account: *{sp_name or 'sconosciuto'}*\n"
            f"Piano rilevato: *{product}*\n\n"
            "I comandi play/pause/next richiedono\n"
            "*Spotify Premium*. Hai connesso l'account\n"
            "sbagliato? Usa /menu → Riconnetti.\n\n"
            "⚠️ _Il mining e le playlist funzionano_\n"
            "_solo con account Premium._"
        )

    threading.Thread(target=_async_notify, args=(tid, confirm_msg), daemon=True).start()
    # Manda anche il menu completo dopo 2 secondi
    def _send_menu_after(tid_inner, user_inner):
        import time as _time
        _time.sleep(2)
        mining  = bool((user_inner or {}).get("mining_active"))
        menu_txt = (
            f"{hdr_menu(user_inner)}\n\n"
            f"{mining_status_line(mining)}\n"
            f"`▸ scegli un'opzione`"
        )
        _run_async(_send_photo(tid_inner, menu_txt, main_kb(user_inner)))
    threading.Thread(target=_send_menu_after, args=(tid, db_get(tid)), daemon=True).start()

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
            stopped_txt = (
                "⏹️  *Nessuna riproduzione*\n"
                "`· · · · · · · · ·`\n"
                "⚫ Mine Nackles in pausa"
            )
            _run_async(_update_now_playing(tid, stopped_txt))
            # Aggiorna la caption del menu foto
            updated_user = db_get(tid)
            _run_async(_update_menu_caption(tid, updated_user))
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

        now_playing_txt = (
            f"▶️  *{track}*\n"
            f"    {artist}\n"
            "`· · · · · · · · ·`\n"
            "⛏️ Mine Nackles attivo"
        )
        _run_async(_update_now_playing(tid, now_playing_txt))
        # Aggiorna anche la caption sotto l'immagine Acki Jewels
        updated_user = db_get(tid)
        _run_async(_update_menu_caption(tid, updated_user))

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

def menu_row():
    """Riga standard con tasto Menu — appare in ogni schermata."""
    return [InlineKeyboardButton("🏠 Menu", callback_data="back")]

def firma() -> str:
    return "\n`                — Acki Jewels 💎`"

def hdr_main() -> str:
    return (
        f"`{SEP}`\n"
        "    🐝  *A C K I   N A C K I*\n"
        "    ⛏️  *L I S T E N  &  M I N E*\n"
        f"`{SEP}`"
    )

def hdr_menu(user: dict | None = None) -> str:
    """Header menu: mostra brano+artista se in riproduzione, altrimenti titolo standard."""
    last = (user or {}).get("last_track", "") if user else ""
    if last and " — " in last:
        artist, track = last.split(" — ", 1)
        return (
            f"`{SEP}`\n"
            f"▶️  *{track}*\n"
            f"🔴  {artist}\n"
            f"`{SEP}`"
        )
    return (
        f"`{SEP}`\n"
        "  🐝  *LISTEN & MINE*  ⛏️\n"
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


# -------------------------------------------------------
# TELEGRAM — send helpers
# -------------------------------------------------------

def _sync_player_state(user: dict) -> dict:
    """Sincronizza shuffle/repeat dal player reale di Spotify. Ritorna user aggiornato."""
    if not user or not user.get("access_token"):
        return user
    tid = user["telegram_id"]
    player = sp_get(user, "/me/player")
    if player and not player.get("_err") and not player.get("_204"):
        real_repeat  = player.get("repeat_state", "off")
        real_shuffle = player.get("shuffle_state", False)
        db_set(tid, repeat_mode=real_repeat, shuffle_on=1 if real_shuffle else 0)
        return db_get(tid)
    return user

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

async def _update_now_playing(tid: int, text: str):
    """Non manda più messaggi separati. Aggiorna solo la caption del menu."""
    # Tutto il feedback va nella caption della foto menu
    user = db_get(tid)
    if user:
        await _update_menu_caption(tid, user)

async def _edit(q, txt: str = "", markup=None):
    """Modifica il messaggio sia se è foto (caption) che testo normale.
    Se txt è vuoto, modifica solo il markup senza toccare il testo."""
    if txt:
        try:
            await q.edit_message_caption(caption=txt, parse_mode="Markdown", reply_markup=markup)
            return
        except Exception:
            pass
        try:
            await q.edit_message_text(text=txt, parse_mode="Markdown", reply_markup=markup)
            return
        except Exception as e:
            log.error(f"Edit error: {e}")
    else:
        # Solo aggiorna la tastiera
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except Exception as e:
            log.error(f"Edit markup error: {e}")

async def _send_photo(tid: int, caption: str, markup=None):
    """Manda il messaggio con l'immagine Acki Jewels come header.
    Salva il message_id come menu_msg_id per aggiornarlo dopo."""
    if not _tg_app:
        return
    try:
        if os.path.exists(ACKI_IMAGE):
            with open(ACKI_IMAGE, "rb") as img:
                sent = await _tg_app.bot.send_photo(
                    chat_id=tid,
                    photo=img,
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                db_set(tid, menu_msg_id=sent.message_id)
                return
        await _send(tid, caption, markup)
    except Exception as e:
        log.error(f"Send photo error a {tid}: {e}")
        await _send(tid, caption, markup)


async def _update_menu_caption(tid: int, user: dict):
    """Aggiorna la caption del messaggio foto menu con il brano corrente."""
    if not _tg_app:
        return
    msg_id = (user or {}).get("menu_msg_id", 0) or 0
    if not msg_id:
        return
    mining = bool(user and user.get("mining_active"))
    txt = (
        f"{hdr_menu(user)}\n\n"
        f"{mining_status_line(mining)}\n"
        f"`▸ scegli un'opzione`"
    )
    try:
        await _tg_app.bot.edit_message_caption(
            chat_id=tid,
            message_id=msg_id,
            caption=txt,
            parse_mode="Markdown",
            reply_markup=main_kb(user)
        )
    except Exception as e:
        log.debug(f"Menu caption update skipped: {e}")

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
        # Shuffle + Repeat — emoji cambiano quando attivi
        shuffle_on  = bool(user and user.get("shuffle_on"))
        repeat_mode = (user or {}).get("repeat_mode", "off")
        rows.append([
            InlineKeyboardButton(
                "🔀 ● Shuffle ON" if shuffle_on else "🔀 Shuffle",
                callback_data="shuffle_toggle"
            ),
            InlineKeyboardButton(
                "🔂 ● 1 brano"   if repeat_mode == "track"   else
                "🔁 ● Playlist"  if repeat_mode == "context" else
                "🔁 Repeat",
                callback_data="repeat_toggle"
            ),
        ])
        # Apri Spotify + Disconnetti
        # Universal link: apre l'app su mobile se installata, altrimenti web
        rows.append([
            InlineKeyboardButton("🎧 Apri Spotify", url="https://open.spotify.com"),
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
        user   = _sync_player_state(user)
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
            sent = await update.message.reply_photo(
                photo=img,
                caption=txt,
                parse_mode="Markdown",
                reply_markup=main_kb(user)
            )
            db_set(tid, menu_msg_id=sent.message_id)
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=main_kb(user))

async def h_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid  = update.effective_user.id
    user = db_get(tid)
    user = _sync_player_state(user)
    mining = bool(user and user.get("mining_active"))
    txt = (
        f"{hdr_menu(user)}\n\n"
        f"{mining_status_line(mining)}\n"
        f"`▸ scegli un'opzione`"
    )
    if os.path.exists(ACKI_IMAGE):
        with open(ACKI_IMAGE, "rb") as img:
            sent = await update.message.reply_photo(
                photo=img,
                caption=txt,
                parse_mode="Markdown",
                reply_markup=main_kb(user)
            )
            db_set(tid, menu_msg_id=sent.message_id)
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
        try:
            await message.edit_caption(caption=txt, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            try:
                await message.edit_text(text=txt, parse_mode="Markdown", reply_markup=kb)
            except Exception as e:
                log.error(f"Stats edit error: {e}")
    else:
        await message.reply_text(text=txt, parse_mode="Markdown", reply_markup=kb)

# -------------------------------------------------------
# Helper: azioni player con controllo device
# -------------------------------------------------------
async def _player_action(q, user: dict, action: str):
    """Esegue un'azione player. Se device disponibile ma non attivo, lo attiva."""
    tid = user["telegram_id"]

    # Controlla device disponibili
    devices_data = sp_get(user, "/me/player/devices")
    devices = (devices_data or {}).get("devices", [])
    active  = [d for d in devices if d.get("is_active")]

    if not devices:
        await q.answer()
        await _edit(q, 
            "📱 *Spotify non è aperto su nessun dispositivo.*\n\n"
            "Apri l'app Spotify sul telefono o PC\n"
            "(basta aprirla, non serve avviare musica),\n"
            "poi torna qui e premi ▶️\n\n"
            "👉 [Apri Spotify](https://open.spotify.com)",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Riprova", callback_data="play"),
                InlineKeyboardButton("🔙 Menu",    callback_data="back"),
            ]])
        )
        return

    # Usa il device attivo, oppure trasferisci il playback al primo disponibile
    device_id = (active[0] if active else devices[0])["id"]

    # Se nessun device è attivo, trasferisci il playback prima di tutto
    if not active:
        log.info(f"Nessun device attivo, trasferisco a {device_id}")
        sp_put(user, "/me/player", body={"device_ids": [device_id], "play": False})
        time.sleep(0.3)  # aspetta che Spotify attivi il device

    if action == "play":
        # Prima prova: riprendi ciò che c'era in play
        res = sp_put(user, "/me/player/play", params={"device_id": device_id})
        status = (res or {}).get("_status", 0)

        # Se 404 = nessun contesto attivo, prova con recently played
        if status == 404:
            log.info("Play 404 — provo con recently played")
            recent = sp_get(user, "/me/player/recently-played", params={"limit": 1})
            ctx_uri = None
            if recent and not recent.get("_err"):
                items = recent.get("items", [])
                if items:
                    ctx = items[0].get("context")
                    if ctx and ctx.get("uri"):
                        ctx_uri = ctx["uri"]

            if ctx_uri:
                # Avvia l'ultimo contesto (playlist/album)
                res = sp_put(user, "/me/player/play",
                             params={"device_id": device_id},
                             body={"context_uri": ctx_uri})
            else:
                # Fallback: avvia i brani salvati (Liked Songs)
                liked = sp_get(user, "/me/tracks", params={"limit": 20})
                uris = []
                if liked and not liked.get("_err"):
                    for item in liked.get("items", []):
                        t = item.get("track")
                        if t and t.get("uri"):
                            uris.append(t["uri"])
                if uris:
                    res = sp_put(user, "/me/player/play",
                                 params={"device_id": device_id},
                                 body={"uris": uris})
                else:
                    await q.answer("⚠️ Nessun brano trovato. Apri Spotify e avvia qualcosa.", show_alert=True)
                    return

            status = (res or {}).get("_status", 0)

    elif action == "pause":
        res = sp_put(user, "/me/player/pause", params={"device_id": device_id})
        status = (res or {}).get("_status", 0)
    elif action == "next":
        res = sp_post(user, "/me/player/next")
        status = (res or {}).get("_status", 0)
    elif action == "prev":
        res = sp_post(user, "/me/player/previous")
        status = (res or {}).get("_status", 0)
    else:
        return

    if status in (200, 202, 204):
        icons = {"play": "▶️ Avviata!", "pause": "⏸️ Pausa.", "next": "⏭️ Avanti!", "prev": "⏮️ Indietro!"}
        await q.answer(icons.get(action, "✅"), show_alert=False)
    elif status == 403:
        await q.answer("⚠️ Serve Spotify Premium.", show_alert=True)
    elif status == 404:
        await q.answer()
        await _edit(q, 
            "📱 *Nessun dispositivo attivo trovato.*\n\n"
            "Apri l'app Spotify, poi torna qui e riprova.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Riprova", callback_data="play"),
                InlineKeyboardButton("🔙 Menu", callback_data="back")
            ]])
        )
    else:
        await q.answer(f"⚠️ Errore ({status}). Riprova.", show_alert=True)


async def _toggle_shuffle(q, user: dict):
    """Attiva/disattiva shuffle su Spotify. Solo toast + aggiorna bottoni."""
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
    log.info(f"Shuffle toggle → state={state_str} device={device_id} result={status}")

    if status in (200, 202, 204):
        db_set(tid, shuffle_on=1 if new_state else 0)
        updated_user = db_get(tid)
        label = "🔀 Shuffle ON" if new_state else "🔀 Shuffle OFF"
        await q.answer(label, show_alert=False)
        await _edit(q, markup=main_kb(updated_user))
    elif status == 403:
        await q.answer("⚠️ Serve Spotify Premium per Shuffle.", show_alert=True)
    else:
        await q.answer(f"⚠️ Errore Spotify ({status})", show_alert=True)


async def _toggle_repeat(q, user: dict):
    """Cicla modalità repeat sincronizzata con Spotify reale."""
    tid = user["telegram_id"]

    # Leggi lo stato REALE da Spotify
    player = sp_get(user, "/me/player")
    if player and not player.get("_err") and not player.get("_204"):
        real_repeat = player.get("repeat_state", "off")
        real_shuffle = player.get("shuffle_state", False)
        # Sync DB con realtà
        db_set(tid, repeat_mode=real_repeat, shuffle_on=1 if real_shuffle else 0)
    else:
        real_repeat = (user or {}).get("repeat_mode", "off")

    cycle    = {"off": "context", "context": "track", "track": "off"}
    new_mode = cycle.get(real_repeat, "off")
    labels   = {"off": "🔁 Repeat OFF", "context": "🔁 Repeat Playlist", "track": "🔂 Repeat 1 Brano"}

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
    log.info(f"Repeat toggle → mode={new_mode} (was {real_repeat}) device={device_id} result={status}")

    if status in (200, 202, 204):
        db_set(tid, repeat_mode=new_mode)
        updated_user = db_get(tid)
        await q.answer(labels.get(new_mode, "✅"), show_alert=False)
        await _edit(q, markup=main_kb(updated_user))
    elif status == 403:
        await q.answer("⚠️ Serve Spotify Premium per Repeat.", show_alert=True)
    else:
        await q.answer(f"⚠️ Errore Spotify ({status})", show_alert=True)


async def h_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    tid  = update.effective_user.id
    data = q.data
    user = db_get(tid)

    # Handler che gestiscono q.answer() internamente (toast personalizzati)
    if data == "shuffle_toggle":
        await _toggle_shuffle(q, user)
        return
    if data == "repeat_toggle":
        await _toggle_repeat(q, user)
        return
    if data in ("play", "pause", "next", "prev"):
        await _player_action(q, user, data)
        return

    # Tutti gli altri: answer vuoto subito
    await q.answer()

    # --- Connetti Spotify ---
    if data == "connect":
        state           = secrets.token_urlsafe(16)
        _pending[state] = tid
        params = {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "state": state,
            "show_dialog": "true",   # forza consenso → scope sempre aggiornati
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
        user_upd = db_get(tid)
        txt_on = (
            f"{hdr_menu(user_upd)}\n\n"
            "⛏️ *Mine Nackles ATTIVO!*\n"
            "`▸ scegli un'opzione`"
        )
        await _edit(q, txt_on, markup=main_kb(user_upd))

    elif data == "mining_off":
        db_set(tid, mining_active=0, last_track="")
        if tid in _session_start:
            mins = max(1, int((now_ts() - _session_start.pop(tid)) / 60))
            stats_increment(tid, minutes=mins)
        user_upd = db_get(tid)
        txt_off = (
            f"{hdr_menu(user_upd)}\n\n"
            "⚫ *Mine Nackles SOSPESO*\n"
            "`▸ scegli un'opzione`"
        )
        await _edit(q, txt_off, markup=main_kb(user_upd))

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
                "Apri l'app Spotify, poi torna qui e riprova.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
                    InlineKeyboardButton("🏠 Menu",     callback_data="back"),
                ]])
            )
            return

        device_id = (active[0] if active else devices[0])["id"]
        if not active:
            sp_put(user, "/me/player", body={"device_ids": [device_id], "play": False})
            time.sleep(0.3)
        res = sp_put(user, "/me/player/play",
                     params={"device_id": device_id},
                     body={"context_uri": uri})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Playlist avviata!")
        else:
            await _edit(q, 
                f"⚠️ Non riesco ad avviare la playlist.\n\n"
                f"Prova ad aprirla: [Apri in Spotify]({spotify_url})",
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
                "Apri l'app Spotify, poi torna qui e riprova.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
                    InlineKeyboardButton("🏠 Menu",     callback_data="back"),
                ]])
            )
            return

        device_id = (active[0] if active else devices[0])["id"]
        if not active:
            sp_put(user, "/me/player", body={"device_ids": [device_id], "play": False})
            time.sleep(0.3)
        res = sp_put(user, "/me/player/play",
                     params={"device_id": device_id},
                     body={"uris": [uri]})
        if res and res["_status"] in (200, 202, 204):
            await q.answer("▶️ Brano avviato!")
        else:
            await _edit(q, 
                f"⚠️ Non riesco ad avviare il brano.\n\n"
                f"Prova ad aprirlo: [Apri in Spotify]({spotify_url})",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists")
                ]])
            )

    elif data == "disconnect":
        db_set(tid, access_token=None, refresh_token=None,
               mining_active=0, last_track="", shuffle_on=0, repeat_mode="off",
               now_playing_msg_id=0)
        await _edit(q,
            f"`{SEP}`\n"
            "🔌 *Disconnesso da Spotify*\n"
            f"`{SEP}`\n\n"
            "Usa il bottone per riconnetterti.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎵 Connetti Spotify", callback_data="connect")
            ]])
        )

    elif data == "reconnect":
        # Disconnette silenziosamente e avvia subito il flusso di connessione
        db_set(tid, access_token=None, refresh_token=None,
               mining_active=0, last_track="", shuffle_on=0, repeat_mode="off",
               now_playing_msg_id=0)
        # Rimanda all'handler connect riusando lo stesso codice
        import secrets as _sec, urllib.parse as _urlp
        state           = _sec.token_urlsafe(16)
        _pending[state] = tid
        params = {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "state": state,
            "show_dialog": "true",   # forza consenso → scope sempre aggiornati
        }
        url = SPOTIFY_AUTH_URL + "?" + _urlp.urlencode(params)
        txt = (
            f"`{SEP}`\n"
            "🔐 *RICONNETTI SPOTIFY PREMIUM*\n"
            f"`{SEP}`\n\n"
            "Token aggiornato — autorizza di nuovo:\n\n"
            f"👉 [🎧 Autorizza Spotify Premium]({url})\n\n"
            "⚠️ _Assicurati di usare l'account Premium_"
            f"{firma()}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💎 Ho autorizzato!", callback_data="check_auth")
        ]])
        await _edit(q, txt, markup=kb)

    elif data == "back":
        user = db_get(tid)
        user = _sync_player_state(user)
        mining = bool(user and user.get("mining_active"))
        txt = (
            f"{hdr_menu(user)}\n\n"
            f"{mining_status_line(mining)}\n"
            f"`▸ scegli un'opzione`"
        )
        await _edit(q, txt, markup=main_kb(user))

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

    await _edit(q, txt,
        markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Aggiorna", callback_data="status"),
            InlineKeyboardButton("📊 Stats",    callback_data="stats"),
            InlineKeyboardButton("🏠 Menu",     callback_data="back"),
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

    if not items:
        await _edit(q,
            f"{hdr_playlist()}\n\n"
            "📋 *Nessuna playlist trovata.*\n\n"
            "_Assicurati di avere playlist su Spotify_",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Ricarica",  callback_data="playlists"),
                InlineKeyboardButton("🏠 Menu",      callback_data="back"),
            ]])
        )
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
    rows.append(menu_row())
    await _edit(q,
        f"{hdr_playlist()}\n\n"
        f"_Pag. {page+1}/{pages} — {total} playlist_\n\n"
        f"Scegli una playlist:",
        markup=InlineKeyboardMarkup(rows)
    )

# -------------------------------------------------------
# Helper: brani di una playlist
# -------------------------------------------------------
async def _edit_playlist_tracks(q, user, pl_id: str, page=0):
    if not user:
        await _edit(q, "❌ Non connesso.")
        return

    limit = 6

    # Usa SOLO /playlists/{id} che restituisce fino a 100 brani embedded
    # Questo bypassa il 403 di /playlists/{id}/tracks in Dev Mode
    pl_data = sp_get(user, f"/playlists/{pl_id}")

    if not pl_data or pl_data.get("_err"):
        err_code = (pl_data or {}).get("_err", "?")
        body_info = str((pl_data or {}).get("_body", ""))[:120]
        log.error(f"Playlist error: pl_id={pl_id} err={err_code}")

        if str(err_code) == "403":
            msg = (
                f"`{SEP}`\n"
                f"🔒 *Errore 403 — Accesso negato*\n"
                f"`{SEP}`\n\n"
                "Spotify blocca l'accesso a questa playlist.\n\n"
                "🔴 *Soluzioni:*\n"
                "1️⃣ Vai su [Spotify Dashboard]"
                "(https://developer.spotify.com/dashboard)\n"
                "2️⃣ Apri la tua app → *Settings*\n"
                "3️⃣ In *User Management* aggiungi\n"
                "   la tua email Spotify\n"
                "4️⃣ Torna qui → *Riconnetti*\n\n"
                "⚠️ _In Development Mode solo utenti_\n"
                "_aggiunti possono usare tutte le API._"
            )
        else:
            msg = (
                f"⚠️ Errore Spotify `{err_code}`:\n"
                f"`{body_info}`\n\n"
                "Riprova o premi Riconnetti."
            )
        await _edit(q, msg,
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Riprova",  callback_data=f"pl:{pl_id}"),
                InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
            ],[
                InlineKeyboardButton("🔌 Riconnetti Spotify", callback_data="reconnect"),
            ],[
                InlineKeyboardButton("🏠 Menu", callback_data="back"),
            ]])
        )
        return

    pl_name   = pl_data.get("name", "Playlist")
    pl_uri    = pl_data.get("uri", "")
    tracks_obj = pl_data.get("tracks") or {}
    all_items = tracks_obj.get("items", [])
    total     = tracks_obj.get("total", len(all_items))

    log.info(f"Playlist OK: {pl_name}, total={total}, embedded={len(all_items)}")

    # Paginazione manuale sugli items embedded
    start = page * limit
    end   = start + limit
    items = all_items[start:end]
    pages = max(1, (min(total, len(all_items)) + limit - 1) // limit)

    rows = []

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

    if len(rows) <= 1 and not items:
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

    # Nota se ci sono più di 100 brani
    extra = ""
    if total > len(all_items):
        extra = f"\n_Mostrati {len(all_items)}/{total} — apri Spotify per tutti_"

    rows.append([
        InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
        InlineKeyboardButton("🏠 Menu",     callback_data="back"),
    ])

    await _edit(q,
        f"{hdr_playlist()}\n\n"
        f"📋 *{pl_name}*  _({total} brani)_\n\n"
        f"Premi ▶️ per avviare:{extra}",
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
