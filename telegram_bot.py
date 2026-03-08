"""
telegram_bot.py v3.1 — Bot Telegram "Listen & Mine"
----------------------------------------------------
Avvio:  python telegram_bot.py
Dipendenze: pip install python-telegram-bot flask requests

Novità v3.1:
- Fix riconoscimento Premium e dispositivi attivi
- Fix visualizzazione brani nelle playlist
- Gestione errori migliorata per shuffle/repeat
- Verifica stato Premium in tempo reale
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
            "is_premium":         "INTEGER DEFAULT -1",
            "has_app":            "INTEGER DEFAULT -1",
            "setup_done":         "INTEGER DEFAULT 0",
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
    try:
        resp_body = r.json() if r.content else {}
    except Exception:
        resp_body = {"_raw": r.text}
    if r.status_code >= 400:
        log.error(f"sp_put {path} params={params} → {r.status_code}: {resp_body}")
    return {"_status": r.status_code, "_body": resp_body}

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
# FUNZIONI AGGIUNTIVE PER STATO PREMIUM E DISPOSITIVI
# -------------------------------------------------------
def check_premium_status(user: dict) -> bool:
    """Verifica se l'utente ha Spotify Premium e aggiorna il DB."""
    if not user or not user.get("access_token"):
        return False
    
    profile = sp_get(user, "/me")
    if profile and not profile.get("_err"):
        product = (profile.get("product") or "").lower()
        is_premium = product == "premium"
        
        # Aggiorna il DB
        db_set(user["telegram_id"], is_premium=1 if is_premium else 0)
        
        if is_premium:
            log.info(f"✅ Utente {user['telegram_id']} confermato Premium")
        else:
            log.info(f"⚠️ Utente {user['telegram_id']} è Free (product: {product})")
        
        return is_premium
    
    return False

def _get_device_id_optional(user: dict) -> str | None:
    """
    Ritorna il device_id se disponibile, altrimenti None.
    Versione migliorata con più tentativi e logging dettagliato.
    """
    if not user or not user.get("access_token"):
        return None
        
    tid = user["telegram_id"]
    
    # Primo tentativo: /me/player per il dispositivo attivo
    player = sp_get(user, "/me/player")
    
    if player and not player.get("_err") and not player.get("_204"):
        dev = player.get("device") or {}
        did = dev.get("id")
        if did:
            log.info(f"[device] Trovato dispositivo attivo: {dev.get('name')} (ID: {did})")
            # Aggiorna lo stato premium se non lo è già
            if dev.get("type") and not user.get("is_premium"):
                # Verifica se è premium in base al tipo di dispositivo
                is_premium = dev.get("type") != "unknown"
                if is_premium:
                    db_set(tid, is_premium=1)
            return did
    
    # Secondo tentativo: lista di tutti i dispositivi
    devices_data = sp_get(user, "/me/player/devices")
    devices = (devices_data or {}).get("devices", [])
    
    if devices:
        # Cerca dispositivo attivo prima
        active_devices = [d for d in devices if d.get("is_active")]
        if active_devices:
            chosen = active_devices[0]
            did = chosen.get("id")
            log.info(f"[device] Trovato dispositivo attivo da lista: {chosen.get('name')}")
            return did
        
        # Se nessun dispositivo attivo, prendi il primo disponibile
        chosen = devices[0]
        did = chosen.get("id")
        log.info(f"[device] Nessun dispositivo attivo, uso: {chosen.get('name')}")
        
        # Tenta di trasferire la riproduzione a questo dispositivo
        if did:
            transfer_res = sp_put(user, "/me/player", body={"device_ids": [did], "play": False})
            if transfer_res and transfer_res.get("_status") in (200, 202, 204):
                log.info(f"[device] Trasferimento a {chosen.get('name')} riuscito")
                time.sleep(1)  # Aspetta che il trasferimento sia completato
            return did
    
    log.warning("[device] Nessun dispositivo trovato")
    return None

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

@_oauth_app.get("/open-spotify")
def open_spotify_redirect():
    """
    Apre l'app Spotify se installata, altrimenti ricade su open.spotify.com.
    Usato dal bottone 'Apri Spotify' nel menu — Telegram accetta solo https://.
    """
    path = request.args.get("path", "")   # opzionale: es. /track/xyz
    web_url = f"https://open.spotify.com/{path}" if path else "https://open.spotify.com"
    # spotify: URI: spotify:track:xyz oppure solo spotify:
    if path:
        # converti path web → URI (es. track/abc → spotify:track:abc)
        parts = [p for p in path.strip("/").split("/") if p]
        spotify_uri = "spotify:" + ":".join(parts) if parts else "spotify:"
    else:
        spotify_uri = "spotify:"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Apri Spotify</title>
  <style>
    body {{ background:#0e0b08; color:#e8a87c; font-family:-apple-system,sans-serif;
           display:flex; align-items:center; justify-content:center;
           min-height:100vh; margin:0; text-align:center; padding:20px; }}
    .msg {{ font-size:20px; margin-bottom:10px; }}
    .sub {{ font-size:13px; color:#888; margin-top:14px; }}
    a {{ color:#c0392b; }}
  </style>
</head>
<body>
  <div>
    <div class="msg">🎧 Apertura Spotify…</div>
    <div class="sub">Se l'app non si apre entro 3 secondi,<br>
      <a href="{web_url}">apri la versione web</a>.
    </div>
  </div>
  <script>
    var fallbackTimer = null;
    var closeTimer    = null;
    var appOpened     = false;

    function returnToTelegram() {{
      // Tenta di chiudere il WebView (funziona in Telegram iOS/Android)
      try {{ window.close(); }} catch(e) {{}}
      // Fallback: apri il bot via deep link tg://
      setTimeout(function() {{
        window.location.href = "tg://resolve?domain=SpoteeBeeBot";
      }}, 150);
    }}

    function openApp() {{
      // Timer fallback web — se dopo 3s siamo ancora qui
      fallbackTimer = setTimeout(function() {{
        if (!appOpened) {{
          window.location.href = "{web_url}";
        }}
      }}, 3000);

      window.location.href = "{spotify_uri}";
    }}

    // La pagina va in background = l'app Spotify si è aperta
    document.addEventListener("visibilitychange", function() {{
      if (document.hidden && !appOpened) {{
        appOpened = true;
        if (fallbackTimer) {{ clearTimeout(fallbackTimer); fallbackTimer = null; }}
        // Quando l'utente torna qui (da Spotify a Telegram),
        // la pagina torna visibile → chiudi subito
      }} else if (!document.hidden && appOpened) {{
        // L'utente è tornato sulla pagina dopo aver aperto Spotify
        // → chiudi il WebView e torna su Telegram
        returnToTelegram();
      }}
    }});

    window.addEventListener("pagehide", function() {{
      appOpened = true;
      if (fallbackTimer) {{ clearTimeout(fallbackTimer); }}
    }});

    openApp();
  </script>
</body>
</html>"""
    from flask import Response
    return Response(html, mimetype="text/html")

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

    # Verifica subito se l'account è Premium via API
    user_data   = db_get(tid)
    profile     = None
    premium_ok  = False
    sp_name     = ""
    product     = "unknown"
    if user_data:
        profile = sp_get(user_data, "/me")
        log.info(f"[OAuth] /me response: {profile}")
        if profile and not profile.get("_err"):
            product    = (profile.get("product") or "").lower()
            premium_ok = product == "premium"
            sp_name    = profile.get("display_name") or profile.get("id") or ""
            # Aggiorna lo status premium nel DB basandosi sull'API reale
            db_set(tid, is_premium=1 if premium_ok else 0)
            log.info(f"[OAuth] Account: {sp_name} / product='{product}' / premium={premium_ok}")
        else:
            log.error(f"[OAuth] /me failed: {profile}")

    # Prepara il messaggio di conferma in base al tipo di account
    user_claimed_premium = bool(user_data and user_data.get("is_premium", 0) == 1)

    # Lingua utente dal parametro state (formato: tid_lang)
    raw_state = request.args.get("state", "")
    _lang_from_state = "it"
    if "_" in raw_state:
        _lang_from_state = raw_state.split("_", 1)[-1]
    lang_cb = _lang(_lang_from_state)

    if premium_ok:
        confirm_msg = (
            f"`{SEP}`\n"
            f"{t('connected_premium', lang_cb)}\n"
            f"`{SEP}`\n\n"
            f"👤 *{sp_name}* ⭐ Premium\n\n"
            f"{t('mining_active', lang_cb)}\n"
            f"{t('controls_enabled', lang_cb)}\n\n"
            f"{t('use_menu', lang_cb)}"
        )
    elif user_claimed_premium and not premium_ok:
        # Aveva detto Premium ma non lo è
        confirm_msg = (
            f"`{SEP}`\n"
            f"{t('not_premium_warning', lang_cb)}\n"
            f"`{SEP}`\n\n"
            f"👤 *{sp_name or 'unknown'}*  (plan: *{product}*)\n\n"
            f"{t('not_premium_body', lang_cb)}\n\n"
            f"{t('use_menu', lang_cb)}"
        )
    else:
        # Free — mining funziona, controlli player attivi
        confirm_msg = (
            f"`{SEP}`\n"
            f"{t('connected_free', lang_cb)}\n"
            f"`{SEP}`\n\n"
            f"👤 *{sp_name or 'unknown'}*  (Free)\n\n"
            f"{t('mining_active', lang_cb)}\n"
            f"{t('not_premium_body', lang_cb)}\n\n"
            f"{t('use_menu', lang_cb)}"
        )

    threading.Thread(target=_async_notify, args=(tid, confirm_msg), daemon=True).start()
    # Manda anche il menu completo dopo 2 secondi
    def _send_menu_after(tid_inner, user_inner):
        import time as _time
        _time.sleep(2)
        menu_txt = (
            f"{hdr_menu(user_inner)}\n\n"
            f"{mining_line_from_user(user_inner)}\n"
            f"`▸ scegli un'opzione`"
        )
        _run_async(_send_photo(tid_inner, menu_txt, main_kb(user_inner)))
    threading.Thread(target=_send_menu_after, args=(tid, db_get(tid)), daemon=True).start()

    # Lingua dalla query string (passata dal bot nel link OAuth state)
    ql = request.args.get("lang", "it")

    titles = {
        "it": "SPOTIFY CONNESSO!",
        "en": "SPOTIFY CONNECTED!",
        "es": "¡SPOTIFY CONECTADO!",
        "fr": "SPOTIFY CONNECTÉ!",
        "ru": "SPOTIFY ПОДКЛЮЧЁН!",
    }
    bodies = {
        "it": "⛏️ Mine Nackles è pronto.<br>Torna su Telegram per iniziare.",
        "en": "⛏️ Mine Nackles is ready.<br>Go back to Telegram to start.",
        "es": "⛏️ Mine Nackles está listo.<br>Vuelve a Telegram para empezar.",
        "fr": "⛏️ Mine Nackles est prêt.<br>Retourne sur Telegram pour commencer.",
        "ru": "⛏️ Mine Nackles готов.<br>Вернись в Telegram, чтобы начать.",
    }
    btn_labels = {
        "it": "📱 Apri Telegram",
        "en": "📱 Open Telegram",
        "es": "📱 Abrir Telegram",
        "fr": "📱 Ouvrir Telegram",
        "ru": "📱 Открыть Telegram",
    }
    pg_title  = titles.get(ql, titles["en"])
    pg_body   = bodies.get(ql, bodies["en"])
    pg_btn    = btn_labels.get(ql, btn_labels["en"])

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Acki Nacki — {pg_title}</title>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{
      background: #0e0b08; color: #e8a87c;
      font-family: -apple-system, sans-serif;
      min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: #1a0d08; border: 1px solid #c0392b55;
      border-radius: 20px; padding: 40px 30px;
      max-width: 400px; width: 90%; text-align: center;
    }}
    .icon {{ font-size: 60px; margin-bottom: 20px; }}
    h2 {{ font-size: 22px; color: #e8a87c; margin-bottom: 10px; letter-spacing: 1px; }}
    .sep {{ color: #c0392b; font-size: 20px; margin: 15px 0; letter-spacing: 4px; }}
    p {{ font-size: 15px; color: #7a5a3a; line-height: 1.6; margin-bottom: 20px; }}
    .btn {{
      display: inline-block;
      background: linear-gradient(135deg, #2481cc, #1a6aaa);
      color: white; text-decoration: none;
      padding: 14px 30px; border-radius: 50px;
      font-size: 16px; font-weight: 700; letter-spacing: 1px; margin: 8px;
      cursor: pointer; border: none; width: 80%;
    }}
    .sig {{ font-size: 11px; color: #3a1a0a; margin-top: 25px; font-style: italic; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h2>{pg_title}</h2>
    <div class="sep">▬▬▬▬▬▬▬▬</div>
    <p>{pg_body}</p>
    <button class="btn" id="tgBtn" onclick="goTelegram()">{pg_btn}</button>
    <p class="sig">— Acki Jewels 💎</p>
  </div>
  <script>
    var alreadyGone = false;

    function goTelegram() {{
      if (alreadyGone) return;
      alreadyGone = true;
      // 1) Prova window.close() — chiude il WebView in-app di Telegram
      try {{ window.close(); }} catch(e) {{}}
      // 2) Dopo 200ms prova tg:// deep link
      setTimeout(function() {{
        window.location.href = "tg://resolve?domain=SpoteeBeeBot";
      }}, 200);
    }}

    // Auto-trigger dopo 1.8s
    setTimeout(function() {{ goTelegram(); }}, 1800);

    // Se la pagina torna visibile dopo essere stata in bg (utente è tornato qui)
    // → ritenta subito la chiusura
    document.addEventListener("visibilitychange", function() {{
      if (!document.hidden && alreadyGone) {{
        try {{ window.close(); }} catch(e) {{}}
        setTimeout(function() {{
          window.location.href = "tg://resolve?domain=SpoteeBeeBot";
        }}, 100);
      }}
    }});
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
        # Lingua utente salvata in cache, default "it"
        _ul = _user_lang.get(tid, "it")
        _run_async(_send(tid, t("token_expired", _ul)))
        return

    if data.get("_204") or not data.get("is_playing"):
        if user.get("last_track"):
            db_set(tid, last_track="")
            if tid in _session_start:
                mins = max(1, int((now_ts() - _session_start.pop(tid)) / 60))
                stats_increment(tid, minutes=mins)
            updated_user = db_get(tid)
            stopped_txt = (
                "⏹️  *Nessuna riproduzione*\n"
                "`· · · · · · · · ·`\n"
                + mining_line_from_user(updated_user)
            )
            _run_async(_update_now_playing(tid, stopped_txt))
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

        updated_user = db_get(tid)
        now_playing_txt = (
            f"▶️  *{track}*\n"
            f"    {artist}\n"
            "`· · · · · · · · ·`\n"
            + mining_line_from_user(updated_user)
        )
        _run_async(_update_now_playing(tid, now_playing_txt))
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

# -------------------------------------------------------
# i18n — 5 lingue: it, en, es, fr, ru
# -------------------------------------------------------
_I18N = {
    "connected_premium": {
        "it": "✅ *SPOTIFY PREMIUM CONNESSO!*",
        "en": "✅ *SPOTIFY PREMIUM CONNECTED!*",
        "es": "✅ *¡SPOTIFY PREMIUM CONECTADO!*",
        "fr": "✅ *SPOTIFY PREMIUM CONNECTÉ!*",
        "ru": "✅ *SPOTIFY PREMIUM ПОДКЛЮЧЁН!*",
    },
    "connected_free": {
        "it": "✅ *SPOTIFY FREE CONNESSO!*",
        "en": "✅ *SPOTIFY FREE CONNECTED!*",
        "es": "✅ *¡SPOTIFY FREE CONECTADO!*",
        "fr": "✅ *SPOTIFY FREE CONNECTÉ!*",
        "ru": "✅ *SPOTIFY FREE ПОДКЛЮЧЁН!*",
    },
    "mining_active": {
        "it": "⛏️ Mining parte automaticamente quando ascolti musica 🎵",
        "en": "⛏️ Mining starts automatically when you listen to music 🎵",
        "es": "⛏️ El mining inicia automáticamente cuando escuchas música 🎵",
        "fr": "⛏️ Le mining démarre automatiquement quand tu écoutes de la musique 🎵",
        "ru": "⛏️ Майнинг запускается автоматически когда ты слушаешь музыку 🎵",
    },
    "controls_enabled": {
        "it": "🎛️ Controlli player completi abilitati!",
        "en": "🎛️ Full player controls enabled!",
        "es": "🎛️ ¡Controles completos del reproductor activados!",
        "fr": "🎛️ Contrôles complets du lecteur activés!",
        "ru": "🎛️ Полное управление плеером включено!",
    },
    "use_menu": {
        "it": "Usa /menu per tutti i controlli",
        "en": "Use /menu for all controls",
        "es": "Usa /menu para todos los controles",
        "fr": "Utilise /menu pour tous les contrôles",
        "ru": "Используй /menu для всех элементов управления",
    },
    "not_premium_warning": {
        "it": "⚠️ *ACCOUNT NON PREMIUM*",
        "en": "⚠️ *ACCOUNT IS NOT PREMIUM*",
        "es": "⚠️ *CUENTA NO ES PREMIUM*",
        "fr": "⚠️ *COMPTE NON PREMIUM*",
        "ru": "⚠️ *АККАУНТ НЕ PREMIUM*",
    },
    "not_premium_body": {
        "it": "I controlli play/pause/next potrebbero essere limitati da Spotify su account Free.\n⛏️ *Mining funziona normalmente!*",
        "en": "Play/pause/next controls may be limited by Spotify on Free accounts.\n⛏️ *Mining works normally!*",
        "es": "Los controles de play/pausa/siguiente pueden estar limitados por Spotify en cuentas Free.\n⛏️ ¡*El mining funciona normalmente!*",
        "fr": "Les contrôles play/pause/suivant peuvent être limités par Spotify sur les comptes Free.\n⛏️ *Le mining fonctionne normalement!*",
        "ru": "Управление play/пауза/далее может быть ограничено Spotify на Free аккаунте.\n⛏️ *Майнинг работает нормально!*",
    },
    "no_auth_yet": {
        "it": "⏳ Autorizzazione non ancora ricevuta.\nAssicurati di aver premuto *Accetta* su Spotify.",
        "en": "⏳ Authorization not received yet.\nMake sure you pressed *Accept* on Spotify.",
        "es": "⏳ Autorización aún no recibida.\nAsegúrate de haber presionado *Aceptar* en Spotify.",
        "fr": "⏳ Autorisation pas encore reçue.\nAssure-toi d'avoir appuyé sur *Accepter* sur Spotify.",
        "ru": "⏳ Авторизация ещё не получена.\nУбедись, что нажал *Принять* в Spotify.",
    },
    "disconnected": {
        "it": "🔌 *Disconnesso da Spotify.*",
        "en": "🔌 *Disconnected from Spotify.*",
        "es": "🔌 *Desconectado de Spotify.*",
        "fr": "🔌 *Déconnecté de Spotify.*",
        "ru": "🔌 *Отключён от Spotify.*",
    },
    "reconnect_btn": {
        "it": "🎵 Connetti Spotify",
        "en": "🎵 Connect Spotify",
        "es": "🎵 Conectar Spotify",
        "fr": "🎵 Connecter Spotify",
        "ru": "🎵 Подключить Spotify",
    },
    "open_spotify_btn": {
        "it": "🎧 Apri Spotify",
        "en": "🎧 Open Spotify",
        "es": "🎧 Abrir Spotify",
        "fr": "🎧 Ouvrir Spotify",
        "ru": "🎧 Открыть Spotify",
    },
    "disconnect_btn": {
        "it": "🔌 Disconnetti",
        "en": "🔌 Disconnect",
        "es": "🔌 Desconectar",
        "fr": "🔌 Déconnecter",
        "ru": "🔌 Отключить",
    },
    "spotify_open_page": {
        "it": "🎧 Apertura Spotify…",
        "en": "🎧 Opening Spotify…",
        "es": "🎧 Abriendo Spotify…",
        "fr": "🎧 Ouverture de Spotify…",
        "ru": "🎧 Открываю Spotify…",
    },
    "spotify_open_fallback": {
        "it": "Se l'app non si apre entro 3 secondi,",
        "en": "If the app doesn't open within 3 seconds,",
        "es": "Si la app no se abre en 3 segundos,",
        "fr": "Si l'app ne s'ouvre pas dans 3 secondes,",
        "ru": "Если приложение не откроется за 3 секунды,",
    },
    "spotify_open_link": {
        "it": "apri la versione web",
        "en": "open the web version",
        "es": "abre la versión web",
        "fr": "ouvre la version web",
        "ru": "открой веб-версию",
    },
    "oauth_success_title": {
        "it": "SPOTIFY CONNESSO!",
        "en": "SPOTIFY CONNECTED!",
        "es": "¡SPOTIFY CONECTADO!",
        "fr": "SPOTIFY CONNECTÉ!",
        "ru": "SPOTIFY ПОДКЛЮЧЁН!",
    },
    "oauth_success_body": {
        "it": "⛏️ Mine Nackles è pronto.<br>Torna su Telegram per iniziare.",
        "en": "⛏️ Mine Nackles is ready.<br>Go back to Telegram to start.",
        "es": "⛏️ Mine Nackles está listo.<br>Vuelve a Telegram para empezar.",
        "fr": "⛏️ Mine Nackles est prêt.<br>Retourne sur Telegram pour commencer.",
        "ru": "⛏️ Mine Nackles готов.<br>Вернись в Telegram, чтобы начать.",
    },
    "oauth_open_telegram": {
        "it": "📱 Apri Telegram",
        "en": "📱 Open Telegram",
        "es": "📱 Abrir Telegram",
        "fr": "📱 Ouvrir Telegram",
        "ru": "📱 Открыть Telegram",
    },
    "premium_needed_title": {
        "it": "🔒 *RICHIEDE PREMIUM*",
        "en": "🔒 *PREMIUM REQUIRED*",
        "es": "🔒 *SE REQUIERE PREMIUM*",
        "fr": "🔒 *PREMIUM REQUIS*",
        "ru": "🔒 *НУЖЕН PREMIUM*",
    },
    "no_device": {
        "it": "⚠️ Apri Spotify e avvia un brano, poi ripremi.",
        "en": "⚠️ Open Spotify and play a track, then try again.",
        "es": "⚠️ Abre Spotify y reproduce una pista, luego intenta de nuevo.",
        "fr": "⚠️ Ouvre Spotify et lance un morceau, puis réessaie.",
        "ru": "⚠️ Открой Spotify и запусти трек, потом повтори.",
    },
    "token_expired": {
        "it": "⚠️ Token Spotify scaduto.\nUsa /start per riconnetterti.",
        "en": "⚠️ Spotify token expired.\nUse /start to reconnect.",
        "es": "⚠️ Token de Spotify caducado.\nUsa /start para reconectarte.",
        "fr": "⚠️ Token Spotify expiré.\nUtilise /start pour te reconnecter.",
        "ru": "⚠️ Токен Spotify истёк.\nИспользуй /start для переподключения.",
    },
}

def _lang(telegram_lang: str | None) -> str:
    """Normalizza il codice lingua Telegram → it/en/es/fr/ru."""
    if not telegram_lang:
        return "it"
    l = telegram_lang.lower()[:2]
    return l if l in ("it", "en", "es", "fr", "ru") else "en"

def t(key: str, lang: str = "it") -> str:
    """Ritorna la stringa tradotta. Fallback su 'en', poi 'it'."""
    d = _I18N.get(key, {})
    return d.get(lang) or d.get("en") or d.get("it") or key

# Cache lang per utente (telegram_id → lang)
_user_lang: dict = {}

def get_lang(tid: int | None = None, telegram_lang: str | None = None) -> str:
    if tid and tid in _user_lang:
        return _user_lang[tid]
    lang = _lang(telegram_lang)
    if tid:
        _user_lang[tid] = lang
    return lang

def menu_row():
    """Riga standard con tasto Menu — appare in ogni schermata."""
    return [InlineKeyboardButton("🏠 Menu", callback_data="back")]

def mining_line_from_user(user: dict | None) -> str:
    """Calcola mining_status_line leggendo user dal DB."""
    if not user:
        return mining_status_line(False, False)
    active     = bool(user.get("mining_active"))
    is_playing = bool(user.get("last_track", ""))   # last_track non vuoto = musica in play
    return mining_status_line(active, is_playing)

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
            f"🔵  {artist}\n"
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

def mining_status_line(active: bool, is_playing: bool = False) -> str:
    """
    active    = utente ha abilitato il mining
    is_playing = musica in riproduzione in questo momento
    Colori: 🔵 musica in play, 🔴 abilitato ma fermo, ⚫ disabilitato
    """
    if active and is_playing:
        return "🔵 *Mine Nackles:* `● IN ASCOLTO`  ⛏️"
    if active:
        return "🔴 *Mine Nackles:* `○ IN ATTESA`  ⛏️"
    return "⚫ *Mine Nackles:* `○ DISABILITATO`"

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
    """
    Edita la caption del messaggio foto persistente (menu_msg_id).
    NON chiama q.answer() — il chiamante lo fa prima se necessario.
    """
    tid = q.from_user.id if hasattr(q, "from_user") else None
    if not tid or not _tg_app:
        return

    u   = db_get(tid)
    mid = (u or {}).get("menu_msg_id", 0) or 0

    if mid:
        try:
            if txt:
                await _tg_app.bot.edit_message_caption(
                    chat_id=tid, message_id=mid,
                    caption=txt, parse_mode="Markdown", reply_markup=markup
                )
            else:
                await _tg_app.bot.edit_message_reply_markup(
                    chat_id=tid, message_id=mid, reply_markup=markup
                )
            return
        except Exception as e:
            err_str = str(e)
            log.warning(f"_edit foto mid={mid}: {e}")
            # 400 Bad Request = Markdown malformato → riprova senza parse_mode
            if "400" in err_str or "Bad Request" in err_str:
                try:
                    clean = txt.replace("*","").replace("`","").replace("_","")
                    await _tg_app.bot.edit_message_caption(
                        chat_id=tid, message_id=mid,
                        caption=clean, reply_markup=markup
                    )
                    return
                except Exception as e2:
                    log.warning(f"_edit plain fallback: {e2}")

    # Foto cancellata → manda nuova e salva id
    try:
        if os.path.exists(ACKI_IMAGE):
            with open(ACKI_IMAGE, "rb") as img:
                sent = await _tg_app.bot.send_photo(
                    chat_id=tid, photo=img,
                    caption=txt or "🐝 Menu",
                    parse_mode="Markdown", reply_markup=markup
                )
        else:
            sent = await _tg_app.bot.send_message(
                chat_id=tid, text=txt or "🐝 Menu",
                parse_mode="Markdown", reply_markup=markup
            )
        db_set(tid, menu_msg_id=sent.message_id)
    except Exception as e:
        log.error(f"_edit new photo error: {e}")

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
        # Fallback testo: salva menu_msg_id ugualmente
        try:
            sent = await _tg_app.bot.send_message(
                chat_id=tid, text=caption,
                parse_mode="Markdown", reply_markup=markup
            )
            db_set(tid, menu_msg_id=sent.message_id)
        except Exception as e2:
            log.error(f"Send text fallback error a {tid}: {e2}")
    except Exception as e:
        log.error(f"Send photo error a {tid}: {e}")
        try:
            sent = await _tg_app.bot.send_message(
                chat_id=tid, text=caption,
                parse_mode="Markdown", reply_markup=markup
            )
            db_set(tid, menu_msg_id=sent.message_id)
        except Exception as e2:
            log.error(f"Send text fallback2 error a {tid}: {e2}")


async def _update_menu_caption(tid: int, user: dict):
    """Aggiorna la caption del messaggio foto menu con il brano corrente."""
    if not _tg_app:
        return
    msg_id = (user or {}).get("menu_msg_id", 0) or 0
    if not msg_id:
        # Nessun menu msg salvato → manda nuovo e salva id
        try:
            txt_new = (
                f"{hdr_menu(user)}\n\n"
                f"{mining_line_from_user(user)}\n"
                f"`▸ scegli un'opzione`"
            )
            if os.path.exists(ACKI_IMAGE):
                with open(ACKI_IMAGE, "rb") as img:
                    sent = await _tg_app.bot.send_photo(
                        chat_id=tid, photo=img, caption=txt_new,
                        parse_mode="Markdown", reply_markup=main_kb(user)
                    )
            else:
                sent = await _tg_app.bot.send_message(
                    chat_id=tid, text=txt_new,
                    parse_mode="Markdown", reply_markup=main_kb(user)
                )
            db_set(tid, menu_msg_id=sent.message_id)
        except Exception as e:
            log.error(f"_update_menu_caption new msg error {tid}: {e}")
        return
    txt = (
        f"{hdr_menu(user)}\n\n"
        f"{mining_line_from_user(user)}\n"
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
    authed  = bool(user and user.get("access_token"))
    mining  = bool(user and user.get("mining_active"))
    premium = bool(user and user.get("is_premium", 0) == 1)
    rows    = []

    if not authed:
        rows.append([InlineKeyboardButton("🎵 Connetti Spotify", callback_data="connect")])
    else:
        # Riga 1: stato, stats, playlist (tutti)
        rows.append([
            InlineKeyboardButton("🔵 Stato" if bool(user and user.get("last_track")) else "🔴 Stato", callback_data="status"),
            InlineKeyboardButton("📊 Stats",   callback_data="stats"),
            InlineKeyboardButton("🎼 Playlist", callback_data="playlists"),
        ])
        # Riga 2: mining on/off (tutti)
        rows.append([InlineKeyboardButton(
            "⏸ Sospendi Mine Nackles" if mining else "⛏️ Avvia Mine Nackles",
            callback_data="mining_off" if mining else "mining_on"
        )])

        # Controlli riproduzione — tutti gli utenti
        rows.append([
            InlineKeyboardButton("⏮",  callback_data="prev"),
            InlineKeyboardButton("▶",  callback_data="play"),
            InlineKeyboardButton("⏸",  callback_data="pause"),
            InlineKeyboardButton("⏭",  callback_data="next"),
        ])
        if premium:
            # Shuffle + Repeat — solo Premium
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

        # Apri Spotify + Disconnetti (tutti)
        # Se utente ha l'app → /open-spotify fa deep-link a spotify:
        # Altrimenti → diretto a open.spotify.com
        has_app = bool(user and user.get("has_app", -1) == 1)
        spotify_btn_url = (
            f"{PUBLIC_URL}/open-spotify" if (has_app and PUBLIC_URL)
            else "https://open.spotify.com"
        )
        rows.append([
            InlineKeyboardButton("🎧 Apri Spotify", url=spotify_btn_url),
            InlineKeyboardButton("🔌 Disconnetti",  callback_data="disconnect"),
        ])
    return InlineKeyboardMarkup(rows)

# -------------------------------------------------------
# HANDLERS
# -------------------------------------------------------

# ── Onboarding text and keyboards ──
def _onboard_welcome_txt(name: str) -> str:
    return (
        f"`{SEP}`\n"
        "    🐝  *A C K I   N A C K I*\n"
        "    ⛏️  *L I S T E N  &  M I N E*\n"
        f"`{SEP}`\n\n"
        f"👋 Hey *{name}*! Welcome!\n\n"
        "`▸ BEFORE WE START`\n"
        "I need to know a couple of things\n"
        "to set everything up correctly.\n\n"
        "🎧 *Do you have Spotify Premium?*"
        f"{firma()}"
    )

def _onboard_premium_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, I have Premium", callback_data="setup_premium_yes")],
        [InlineKeyboardButton("❌ No, I have Free", callback_data="setup_premium_no")],
    ])

def _onboard_app_txt() -> str:
    return (
        f"`{SEP}`\n"
        "    🐝  *SETUP — STEP 2/2*\n"
        f"`{SEP}`\n\n"
        "📱 *Do you have the Spotify app*\n"
        "*installed on your phone?*\n\n"
        "_The app is needed so you can_\n"
        "_control playback from Telegram._"
        f"{firma()}"
    )

def _onboard_app_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, it's installed", callback_data="setup_app_yes")],
        [InlineKeyboardButton("❌ No, I don't have it", callback_data="setup_app_no")],
    ])

def _onboard_install_txt() -> str:
    return (
        f"`{SEP}`\n"
        "    📲  *INSTALL SPOTIFY FIRST*\n"
        f"`{SEP}`\n\n"
        "You need the Spotify app to use\n"
        "Listen & Mine.\n\n"
        "📥 Download it here:\n"
        "▸ [Google Play Store](https://play.google.com/store/apps/details?id=com.spotify.music)\n"
        "▸ [Apple App Store](https://apps.apple.com/app/spotify/id324684580)\n\n"
        "Once installed, open it, log in,\n"
        "then come back and press /start"
        f"{firma()}"
    )

def _onboard_no_premium_txt() -> str:
    """Messaggio per utenti Free — mining funziona, controlli no."""
    return (
        f"`{SEP}`\n"
        "    ⚠️  *SPOTIFY FREE — LIMITED MODE*\n"
        f"`{SEP}`\n\n"
        "⛏️ *Mining works* with Free!\n"
        "The bot will track what you listen to\n"
        "and count your Nackles.\n\n"
        "❌ *What won't work:*\n"
        "Play/Pause/Next/Prev controls\n"
        "(these require Premium)\n\n"
        "✅ *What works:*\n"
        "Mining, Stats, Playlist view\n\n"
        "_Upgrade to Premium anytime and_\n"
        "_press Reconnect to unlock all controls._\n\n"
        "🎵 Ready to connect Spotify?"
        f"{firma()}"
    )


async def h_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid   = update.effective_user.id
    name  = update.effective_user.first_name or "utente"
    tg_lang = update.effective_user.language_code or "it"
    lang    = get_lang(tid, tg_lang)
    db_set(tid,
           username   = update.effective_user.username or "",
           first_name = name)
    _user_lang[tid] = lang
    user   = db_get(tid)
    authed = bool(user and user.get("access_token"))
    setup  = bool(user and user.get("setup_done"))

    # ── Se già connesso: mostra menu normale ──
    if authed:
        user   = _sync_player_state(user)
        total  = stats_get_total(tid)

        def fmt(m): return f"{m//60}h {m%60}min" if m >= 60 else f"{m} min"

        txt = (
            f"{hdr_main()}\n\n"
            f"👋 Bentornato *{name}*!\n\n"
            f"`▸ MINE NACKLES`\n"
            f"⛏️  Sessioni:    *{total['sessions']}*\n"
            f"🎵  Brani:       *{total['tracks_heard']}*\n"
            f"⏱️  Tempo totale: *{fmt(total['mining_minutes'])}*\n"
            f"📆  Giorni attivi: *{total['days_active']}*\n\n"
            f"{mining_line_from_user(user)}"
            f"{firma()}"
        )
        kb  = main_kb(user)
        mid = (user or {}).get("menu_msg_id", 0) or 0
        if mid:
            # Riusa il messaggio menu esistente (foto rimane come sfondo)
            try:
                await _tg_app.bot.edit_message_caption(
                    chat_id=tid, message_id=mid,
                    caption=txt, parse_mode="Markdown", reply_markup=kb
                )
                return
            except Exception:
                pass  # Messaggio cancellato → manda nuovo
        # Manda nuovo messaggio con foto
        if os.path.exists(ACKI_IMAGE):
            with open(ACKI_IMAGE, "rb") as img:
                sent = await update.message.reply_photo(
                    photo=img, caption=txt,
                    parse_mode="Markdown", reply_markup=kb
                )
                db_set(tid, menu_msg_id=sent.message_id)
        else:
            sent = await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)
            db_set(tid, menu_msg_id=sent.message_id)
        return

    # ── Onboarding: step 1 — chiedi Premium ──
    txt = _onboard_welcome_txt(name)
    if os.path.exists(ACKI_IMAGE):
        with open(ACKI_IMAGE, "rb") as img:
            sent = await update.message.reply_photo(
                photo=img, caption=txt,
                parse_mode="Markdown", reply_markup=_onboard_premium_kb()
            )
            db_set(tid, menu_msg_id=sent.message_id)
    else:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=_onboard_premium_kb())

async def h_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid  = update.effective_user.id
    user = db_get(tid)
    user = _sync_player_state(user)
    txt  = (
        f"{hdr_menu(user)}\n\n"
        f"{mining_line_from_user(user)}\n"
        f"`▸ scegli un'opzione`"
    )
    kb  = main_kb(user)
    mid = (user or {}).get("menu_msg_id", 0) or 0
    if mid:
        try:
            await _tg_app.bot.edit_message_caption(
                chat_id=tid, message_id=mid,
                caption=txt, parse_mode="Markdown", reply_markup=kb
            )
            return
        except Exception:
            pass
    if os.path.exists(ACKI_IMAGE):
        with open(ACKI_IMAGE, "rb") as img:
            sent = await update.message.reply_photo(
                photo=img, caption=txt,
                parse_mode="Markdown", reply_markup=kb
            )
            db_set(tid, menu_msg_id=sent.message_id)
    else:
        sent = await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)
        db_set(tid, menu_msg_id=sent.message_id)

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
    """
    Esegue play/pause/next/prev.
    Strategia: prende device_id veloce (1 tentativo, no retry).
    Lo manda con il comando → funziona anche dopo pause su mobile.
    Se non trovato, manda senza → Spotify prova con l'ultimo attivo.
    """
    tid = user["telegram_id"]
    icons = {"play": "▶️", "pause": "⏸️ Pausa.", "next": "⏭️ Avanti!", "prev": "⏮️ Indietro!"}

    # Prendi device_id veloce — 1 sola chiamata, nessun retry/sleep
    device_id = _get_device_id_optional(user)
    params    = {"device_id": device_id} if device_id else {}
    log.info(f"[player] action={action} device_id={device_id or 'none'}")

    if action == "play":
        res    = sp_put(user, "/me/player/play", params=params)
        status = (res or {}).get("_status", 0)
        log.info(f"[play] → {status}")

        if status == 404:
            # Nessun contesto attivo: riprendi dall'ultima sessione
            log.info("[play] 404 → provo recently-played")
            recent  = sp_get(user, "/me/player/recently-played", params={"limit": 1})
            ctx_uri = None
            if recent and not recent.get("_err"):
                items = recent.get("items", [])
                if items:
                    ctx = items[0].get("context")
                    if ctx and ctx.get("uri"):
                        ctx_uri = ctx["uri"]
            if ctx_uri:
                res    = sp_put(user, "/me/player/play", params=params,
                                body={"context_uri": ctx_uri})
                status = (res or {}).get("_status", 0)
            else:
                # Ultima spiaggia: brani salvati
                liked = sp_get(user, "/me/tracks", params={"limit": 20})
                uris  = [it["track"]["uri"] for it in (liked or {}).get("items", [])
                         if it.get("track") and it["track"].get("uri")]
                if uris:
                    res    = sp_put(user, "/me/player/play", params=params,
                                    body={"uris": uris})
                    status = (res or {}).get("_status", 0)
                else:
                    await q.answer("⚠️ Apri Spotify, avvia un brano, poi ripremi ▶️",
                                   show_alert=True)
                    return

    elif action == "pause":
        res    = sp_put(user, "/me/player/pause", params=params)
        status = (res or {}).get("_status", 0)
        log.info(f"[pause] → {status}")
    elif action == "next":
        res    = sp_post(user, "/me/player/next")
        status = (res or {}).get("_status", 0)
        log.info(f"[next] → {status}")
    elif action == "prev":
        res    = sp_post(user, "/me/player/previous")
        status = (res or {}).get("_status", 0)
        log.info(f"[prev] → {status}")
    else:
        return

    if status in (200, 202, 204):
        await q.answer(icons.get(action, "✅"), show_alert=False)
    elif status == 403:
        # Free mobile: Spotify non permette on-demand playback
        # Premium: token senza i permessi giusti → riconnetti
        is_prem = bool(user.get("is_premium", 0) == 1)
        if is_prem:
            await q.answer("⚠️ Errore permessi — premi Riconnetti Spotify.", show_alert=True)
        else:
            await q.answer(
                "⚠️ Spotify Free limita questa azione su mobile.\n"
                "Su desktop/web funziona normalmente.", show_alert=True)
    elif status == 404:
        # Device non trovato → schermata con istruzioni dettagliate
        has_app = bool(user.get("has_app", -1) == 1)
        spotify_btn_url = (
            f"{PUBLIC_URL}/open-spotify" if (has_app and PUBLIC_URL)
            else "https://open.spotify.com"
        )
        await q.answer()
        await _edit(q,
            f"`{SEP_S}`\n"
            "📱 *Nessun dispositivo trovato*\n"
            f"`{SEP_S}`\n\n"
            "Spotify non vede nessun dispositivo attivo.\n\n"
            "✅ *Come fare:*\n"
            "1️⃣ Apri l'app Spotify\n"
            "2️⃣ Avvia manualmente un brano\n"
            "3️⃣ Torna qui e premi ▶️\n\n"
            "_Dopo la prima volta i controlli_\n"
            "_funzioneranno automaticamente._",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎧 Apri Spotify", url=spotify_btn_url),
            ],[
                InlineKeyboardButton("🔄 Riprova ▶️", callback_data="play"),
                InlineKeyboardButton("🏠 Menu",        callback_data="back"),
            ]])
        )
    else:
        log.error(f"[player] azione={action} status={status} res={res}")
        await q.answer(f"⚠️ Errore Spotify ({status}).", show_alert=True)


async def _toggle_shuffle(q, user: dict):
    """Attiva/disattiva shuffle con gestione errori migliorata."""
    tid = user["telegram_id"]
    
    # Verifica Premium prima di procedere
    if not check_premium_status(user):
        await q.answer("🔒 Shuffle richiede Spotify Premium", show_alert=True)
        return
    
    current = bool(user.get("shuffle_on"))
    new_state = not current
    state_str = "true" if new_state else "false"

    device_id = _get_device_id_optional(user)
    
    if not device_id:
        # Mostra messaggio per aprire Spotify
        await q.answer()
        has_app = bool(user.get("has_app", -1) == 1)
        spotify_url = f"{PUBLIC_URL}/open-spotify" if (has_app and PUBLIC_URL) else "https://open.spotify.com"
        
        await _edit(q,
            f"`{SEP_S}`\n"
            "📱 *Nessun dispositivo attivo*\n"
            f"`{SEP_S}`\n\n"
            "Apri Spotify e avvia un brano, poi riprova.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎧 Apri Spotify", url=spotify_url),
            ],[
                InlineKeyboardButton("🔄 Riprova Shuffle", callback_data="shuffle_toggle"),
                InlineKeyboardButton("🏠 Menu", callback_data="back"),
            ]])
        )
        return

    params_sh = {"state": state_str, "device_id": device_id}
    
    res = sp_put(user, "/me/player/shuffle", params=params_sh)
    status = (res or {}).get("_status", 0)
    
    if status in (200, 202, 204):
        db_set(tid, shuffle_on=1 if new_state else 0)
        updated_user = db_get(tid)
        label = "🔀 Shuffle ON" if new_state else "🔀 Shuffle OFF"
        await q.answer(label, show_alert=False)
        await _edit(q, markup=main_kb(updated_user))
    else:
        await q.answer("⚠️ Errore. Apri Spotify e riprova.", show_alert=True)


async def _toggle_repeat(q, user: dict):
    """Cicla modalità repeat sincronizzata con Spotify reale."""
    tid = user["telegram_id"]
    
    # Verifica Premium prima di procedere
    if not check_premium_status(user):
        await q.answer("🔒 Repeat richiede Spotify Premium", show_alert=True)
        return

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

    device_id = _get_device_id_optional(user)
    
    if not device_id:
        await q.answer()
        has_app = bool(user.get("has_app", -1) == 1)
        spotify_url = f"{PUBLIC_URL}/open-spotify" if (has_app and PUBLIC_URL) else "https://open.spotify.com"
        
        await _edit(q,
            f"`{SEP_S}`\n"
            "📱 *Nessun dispositivo attivo*\n"
            f"`{SEP_S}`\n\n"
            "Apri Spotify e avvia un brano, poi riprova.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎧 Apri Spotify", url=spotify_url),
            ],[
                InlineKeyboardButton("🔄 Riprova Repeat", callback_data="repeat_toggle"),
                InlineKeyboardButton("🏠 Menu", callback_data="back"),
            ]])
        )
        return

    params_rp = {"state": new_mode, "device_id": device_id}
    log.info(f"[repeat] mode={new_mode} device={device_id}")

    res    = sp_put(user, "/me/player/repeat", params=params_rp)
    status = (res or {}).get("_status", 0)

    if status in (200, 202, 204):
        db_set(tid, repeat_mode=new_mode)
        updated_user = db_get(tid)
        await q.answer(labels.get(new_mode, "✅"), show_alert=False)
        await _edit(q, markup=main_kb(updated_user))
    else:
        await q.answer("⚠️ Errore. Apri Spotify e riprova.", show_alert=True)


async def _play_uri(q, user: dict, uri: str, is_playlist: bool = True):
    """Helper per riprodurre URI Spotify."""
    device_id = _get_device_id_optional(user)
    
    if not device_id:
        # Mostra messaggio per aprire Spotify
        await q.answer()
        has_app = bool(user.get("has_app", -1) == 1)
        spotify_url = f"{PUBLIC_URL}/open-spotify" if (has_app and PUBLIC_URL) else "https://open.spotify.com"
        
        await _edit(q,
            f"`{SEP_S}`\n"
            "📱 *Nessun dispositivo attivo*\n"
            f"`{SEP_S}`\n\n"
            "Apri Spotify e avvia un brano, poi riprova.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎧 Apri Spotify", url=spotify_url),
            ],[
                InlineKeyboardButton("🔄 Riprova", callback_data=q.data),
                InlineKeyboardButton("🏠 Menu", callback_data="back"),
            ]])
        )
        return
    
    params = {"device_id": device_id}
    body = {"context_uri": uri} if is_playlist else {"uris": [uri]}
    
    res = sp_put(user, "/me/player/play", params=params, body=body)
    status = (res or {}).get("_status", 0)
    
    if status in (200, 202, 204):
        await q.answer("▶️ Riproduzione avviata!", show_alert=False)
    else:
        await q.answer("⚠️ Errore durante la riproduzione", show_alert=True)


async def h_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    tid  = update.effective_user.id
    data = q.data
    user = db_get(tid)
    lang = get_lang(tid, update.effective_user.language_code)

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
    if data.startswith("playpl:"):
        uri = data[7:]  # rimuovi "playpl:"
        await _play_uri(q, user, uri, is_playlist=True)
        return
    if data.startswith("playtrack:"):
        uri = data[10:]  # rimuovi "playtrack:"
        await _play_uri(q, user, uri, is_playlist=False)
        return

    # Tutti gli altri: answer vuoto subito
    await q.answer()

    # ── ONBOARDING — setup flow ──
    if data == "setup_premium_yes":
        db_set(tid, is_premium=1)
        await _edit(q, _onboard_app_txt(), markup=_onboard_app_kb())
        return

    elif data == "setup_premium_no":
        db_set(tid, is_premium=0)
        await _edit(q, _onboard_app_txt(), markup=_onboard_app_kb())
        return

    elif data == "setup_app_yes":
        db_set(tid, has_app=1, setup_done=1)
        user = db_get(tid)
        premium = bool(user and user.get("is_premium", 0) == 1)
        if premium:
            txt = (
                f"`{SEP}`\n"
                "    ✅  *SETUP COMPLETE!*\n"
                f"`{SEP}`\n\n"
                "🎧 *Premium* + 📱 *App installed*\n"
                "You're all set for the full experience!\n\n"
                "⛏️ Mining + 🎛️ Player controls\n"
                "📊 Stats + 🎼 Playlist management\n\n"
                "🔴 *Now connect your Spotify account:*"
                f"{firma()}"
            )
        else:
            txt = _onboard_no_premium_txt()
        await _edit(q, txt,
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎵 Connect Spotify", callback_data="connect")
            ]])
        )
        return

    elif data == "setup_app_no":
        db_set(tid, has_app=0)
        await _edit(q, _onboard_install_txt(),
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done, I installed it!", callback_data="setup_app_yes"),
            ]])
        )
        return

    elif data == "premium_needed":
        await _edit(q,
            f"`{SEP}`\n"
            "🔒 *PREMIUM REQUIRED*\n"
            f"`{SEP}`\n\n"
            "Player controls (play, pause, next,\n"
            "prev, shuffle, repeat) require\n"
            "*Spotify Premium*.\n\n"
            "⛏️ *Mining works normally* with Free!\n"
            "Just open Spotify, play music, and\n"
            "the bot tracks everything.\n\n"
            "_Upgrade to Premium anytime, then_\n"
            "_press Reconnect to unlock controls._"
            f"{firma()}",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔌 Reconnect (after upgrade)", callback_data="reconnect"),
                InlineKeyboardButton("🏠 Menu", callback_data="back"),
            ]])
        )
        return

    # --- Connetti Spotify ---
    if data == "connect":
        state           = secrets.token_urlsafe(16) + "_" + lang
        _pending[state] = tid
        params = {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "state": state,
            "show_dialog": "true",
        }
        url = SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(params)

        premium = bool(user and user.get("is_premium", 0) == 1)
        if premium:
            txt = (
                f"`{SEP}`\n"
                "🔐 *CONNECT SPOTIFY PREMIUM*\n"
                f"`{SEP}`\n\n"
                "`▸ FOLLOW THESE STEPS`\n"
                "1️⃣  Open the *Spotify app* on your phone\n"
                "2️⃣  Make sure you're logged in with\n"
                "   your *Premium* account\n"
                "3️⃣  Press the link below\n"
                "4️⃣  Press *Accept* on the Spotify page\n"
                "5️⃣  Come back here and press the button\n\n"
                f"👉 [🎧 Authorize Spotify]({url})\n\n"
                "⚠️ _If you see an upgrade page: go back,_\n"
                "_open Spotify, log out and re-login_\n"
                "_with your Premium account._"
                f"{firma()}"
            )
        else:
            txt = (
                f"`{SEP}`\n"
                "🔐 *CONNECT SPOTIFY FREE*\n"
                f"`{SEP}`\n\n"
                "⛏️ Mining mode — limited controls\n\n"
                "`▸ FOLLOW THESE STEPS`\n"
                "1️⃣  Open the *Spotify app* on your phone\n"
                "2️⃣  Press the link below\n"
                "3️⃣  Press *Accept* on the Spotify page\n"
                "4️⃣  Come back here and press the button\n\n"
                f"👉 [🎧 Authorize Spotify]({url})\n\n"
                "_Player controls won't be available._\n"
                "_Mining will track your listening!_"
                f"{firma()}"
            )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💎 Done, check!", callback_data="check_auth")
        ]])
        try:
            await _edit(q, txt, kb)
        except Exception:
            await _edit(q, markup=kb)

    elif data == "check_auth":
        user = db_get(tid)
        if user and user.get("access_token"):
            premium = bool(user.get("is_premium", 0) == 1)
            if premium:
                msg = (
                    f"{t('connected_premium', lang)}\n\n"
                    f"{t('mining_active', lang)}\n"
                    f"{t('controls_enabled', lang)}\n\n"
                    f"{t('use_menu', lang)}"
                )
            else:
                msg = (
                    f"{t('connected_free', lang)}\n\n"
                    f"{t('mining_active', lang)}\n"
                    f"{t('not_premium_body', lang)}\n\n"
                    f"{t('use_menu', lang)}"
                )
            await _edit(q, msg, markup=main_kb(user))
        else:
            await _edit(q,
                t("no_auth_yet", lang),
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
        log.info(f"[mining] ON per {tid} is_premium={db_get(tid).get('is_premium')}")
        user_upd = db_get(tid)
        txt_on = (
            f"{hdr_menu(user_upd)}\n\n"
            f"{mining_line_from_user(user_upd)}\n"
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
            f"{mining_line_from_user(user_upd)}\n"
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

    elif data == "disconnect":
        # Cancella solo il token — mantieni is_premium, has_app, setup_done
        db_set(tid, access_token=None, refresh_token=None,
               mining_active=0, last_track="", shuffle_on=0, repeat_mode="off",
               now_playing_msg_id=0)
        user_upd = db_get(tid)
        premium  = bool(user_upd and user_upd.get("is_premium", 0) == 1)
        await _edit(q,
            f"`{SEP}`\n"
            f"{t('disconnected', lang)}\n"
            f"`{SEP}`",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t("reconnect_btn", lang), callback_data="connect")
            ]])
        )

    elif data == "reconnect":
        # Disconnette silenziosamente e avvia subito il flusso di connessione
        db_set(tid, access_token=None, refresh_token=None,
               mining_active=0, last_track="", shuffle_on=0, repeat_mode="off",
               now_playing_msg_id=0)
        import secrets as _sec, urllib.parse as _urlp
        state           = _sec.token_urlsafe(16) + "_" + lang
        _pending[state] = tid
        params = {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "state": state,
            "show_dialog": "true",
        }
        url = SPOTIFY_AUTH_URL + "?" + _urlp.urlencode(params)
        user_r   = db_get(tid)
        premium_r = bool(user_r and user_r.get("is_premium", 0) == 1)
        if premium_r:
            txt = (
                f"`{SEP}`\n"
                "🔐 *RICONNETTI SPOTIFY PREMIUM*\n"
                f"`{SEP}`\n\n"
                "`▸ SEGUI QUESTI PASSI`\n"
                "1️⃣  Apri l'*app Spotify* sul telefono\n"
                "2️⃣  Assicurati di essere loggato con\n"
                "   il tuo account *Premium*\n"
                "3️⃣  Premi il link qui sotto\n"
                "4️⃣  Premi *Accetta*\n"
                "5️⃣  Torna qui e premi il bottone\n\n"
                f"👉 [🎧 Autorizza Spotify Premium]({url})"
                f"{firma()}"
            )
        else:
            txt = (
                f"`{SEP}`\n"
                "🔐 *RICONNETTI SPOTIFY*\n"
                f"`{SEP}`\n\n"
                "1️⃣  Apri l'*app Spotify* sul telefono\n"
                "2️⃣  Premi il link qui sotto\n"
                "3️⃣  Premi *Accetta*\n"
                "4️⃣  Torna qui e premi il bottone\n\n"
                f"👉 [🎧 Autorizza Spotify]({url})"
                f"{firma()}"
            )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💎 Ho autorizzato, controlla!", callback_data="check_auth")
        ]])
        await _edit(q, txt, markup=kb)

    elif data == "back":
        user = db_get(tid)
        user = _sync_player_state(user)
        txt = (
            f"{hdr_menu(user)}\n\n"
            f"{mining_line_from_user(user)}\n"
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
            f"🔵 {artist}\n"
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
# Helper: brani di una playlist (VERSIONE CORRETTA)
# -------------------------------------------------------
async def _edit_playlist_tracks(q, user, pl_id: str, page=0):
    if not user:
        await _edit(q, "❌ Non connesso.")
        return

    limit = 6
    offset = page * limit
    
    # Ottieni informazioni sulla playlist
    pl_data = sp_get(user, f"/playlists/{pl_id}")
    
    if not pl_data or pl_data.get("_err"):
        err_code = (pl_data or {}).get("_err", "?")
        error_msg = str((pl_data or {}).get("_body", ""))[:120]
        
        if str(err_code) == "403":
            await _edit(q,
                f"`{SEP}`\n"
                "🔒 *Permessi insufficienti*\n"
                f"`{SEP}`\n\n"
                "Il token non ha i permessi per leggere i brani.\n\n"
                "🔑 *Soluzione:* Riconnetti Spotify e assicurati di\n"
                "accettare tutti i permessi richiesti.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Riconnetti Spotify", callback_data="reconnect"),
                ],[
                    InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
                    InlineKeyboardButton("🏠 Menu", callback_data="back"),
                ]])
            )
        else:
            await _edit(q,
                f"⚠️ Errore caricamento playlist: {error_msg}",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Riprova", callback_data=f"pl:{pl_id}"),
                    InlineKeyboardButton("🔙 Indietro", callback_data="back_playlists"),
                ]])
            )
        return

    pl_name = pl_data.get("name", "Playlist")
    pl_uri = pl_data.get("uri", "")
    total_tracks = (pl_data.get("tracks") or {}).get("total", 0)
    pages = max(1, (total_tracks + limit - 1) // limit)

    # Ottieni i brani della playlist - usa l'endpoint corretto
    tracks_data = sp_get(user, f"/playlists/{pl_id}/tracks", 
                        params={
                            "limit": limit, 
                            "offset": offset,
                            "market": "from_token"
                        })

    if not tracks_data or tracks_data.get("_err"):
        err_code = (tracks_data or {}).get("_err", "?")
        await _edit(q,
            f"⚠️ Errore caricamento brani (codice {err_code})",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Riprova", callback_data=f"pl:{pl_id}"),
                InlineKeyboardButton("🔙 Indietro", callback_data="back_playlists"),
            ]])
        )
        return

    items = tracks_data.get("items", [])
    
    if not items:
        await _edit(q,
            f"📋 *{pl_name}*\n\n"
            "Questa playlist non contiene brani.",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
                InlineKeyboardButton("🏠 Menu", callback_data="back"),
            ]])
        )
        return

    rows = []

    # Bottone per avviare tutta la playlist
    if pl_uri:
        rows.append([InlineKeyboardButton(
            f"▶️ AVVIA PLAYLIST COMPLETA",
            callback_data=f"playpl:{pl_uri}"
        )])

    # Aggiungi i brani
    for item in items:
        track = item.get("track")
        if not track or not track.get("uri"):
            continue
            
        track_name = track.get("name", "Sconosciuto")[:25]
        artists = ", ".join([a.get("name", "") for a in track.get("artists", [])])[:20]
        duration_ms = track.get("duration_ms", 0)
        minutes = duration_ms // 60000
        seconds = (duration_ms % 60000) // 1000
        
        rows.append([InlineKeyboardButton(
            f"▶️ {track_name} - {artists} [{minutes}:{seconds:02d}]",
            callback_data=f"playtrack:{track.get('uri')}"
        )])

    # Navigazione pagine
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prec", callback_data=f"pltracks:{pl_id}:{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav_buttons.append(InlineKeyboardButton("Succ ▶️", callback_data=f"pltracks:{pl_id}:{page+1}"))
    
    if len(nav_buttons) > 1:
        rows.append(nav_buttons)

    # Pulsanti di navigazione aggiuntivi
    rows.append([
        InlineKeyboardButton("🔙 Playlist", callback_data="back_playlists"),
        InlineKeyboardButton("🏠 Menu", callback_data="back"),
    ])

    start_idx = offset + 1
    end_idx = min(offset + len(items), total_tracks)
    
    await _edit(q,
        f"`{SEP}`\n"
        f"📋 *{pl_name}*\n"
        f"`{SEP}`\n\n"
        f"_Brani {start_idx}-{end_idx} di {total_tracks}_\n\n"
        f"Seleziona un brano per riprodurlo:",
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
    print("  SPOTEEBEEBOT v3.1 AVVIATO ✅")
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
