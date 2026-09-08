import os
import logging
import sqlite3
from datetime import datetime, timezone

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------

DB_FILE = os.environ.get("DB_FILE", "wallets.db")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ETHERSCAN_API_KEY = os.environ["ETHERSCAN_API_KEY"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))

# Redes soportadas. Cada una indica qué tipo de API usar:
# - "etherscan": API V2 unificada de Etherscan (necesita ETHERSCAN_API_KEY)
# - "blockscout": API de un explorador Blockscout propio (Robinhood Chain no está en Etherscan)
CHAINS = {
    "ethereum":  {"kind": "etherscan", "chainid": 1,     "explorer": "etherscan.io"},
    "bsc":       {"kind": "etherscan", "chainid": 56,    "explorer": "bscscan.com"},
    "polygon":   {"kind": "etherscan", "chainid": 137,   "explorer": "polygonscan.com"},
    "arbitrum":  {"kind": "etherscan", "chainid": 42161, "explorer": "arbiscan.io"},
    "optimism":  {"kind": "etherscan", "chainid": 10,    "explorer": "optimistic.etherscan.io"},
    "base":      {"kind": "etherscan", "chainid": 8453,  "explorer": "basescan.org"},
    "avalanche": {"kind": "etherscan", "chainid": 43114, "explorer": "snowtrace.io"},
    "robinhood": {
        "kind": "blockscout",
        "api_base": "https://robinhoodchain.blockscout.com/api",
        "explorer": "robinhoodchain.blockscout.com",
    },
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wallet-bot")


# ----------------------------------------------------------------------------
# Base de datos (sqlite, no requiere servidor externo)
# ----------------------------------------------------------------------------

def get_conn():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS wallets (
            chat_id INTEGER,
            address TEXT,
            network TEXT,
            last_seen_ts INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, address, network)
        )"""
    )
    conn.commit()
    conn.close()


def is_valid_address(addr: str) -> bool:
    return addr.startswith("0x") and len(addr) == 42


# ----------------------------------------------------------------------------
# Comandos del bot
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 ¡Hola! Soy tu bot de seguimiento de wallets EVM.\n\n"
        "Comandos disponibles:\n"
        "/add <direccion> <red> — Empieza a seguir una wallet\n"
        "/remove <direccion> <red> — Deja de seguir una wallet\n"
        "/list — Muestra las wallets que sigues\n"
        "/redes — Muestra las redes soportadas\n\n"
        "Ejemplo:\n/add 0xabc123... ethereum"
    )
    await update.message.reply_text(text)


async def redes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Redes soportadas: " + ", ".join(CHAINS.keys()))


async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /add <direccion> <red>\nEj: /add 0xabc... ethereum")
        return

    address, network = context.args[0].lower(), context.args[1].lower()

    if not is_valid_address(address):
        await update.message.reply_text(
            "Esa dirección no parece válida (debe empezar por 0x y tener 42 caracteres)."
        )
        return

    if network not in CHAINS:
        await update.message.reply_text("Red no soportada. Usa /redes para ver la lista.")
        return

    conn = get_conn()
    c = conn.cursor()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    c.execute(
        "INSERT OR IGNORE INTO wallets (chat_id, address, network, last_seen_ts) VALUES (?, ?, ?, ?)",
        (update.effective_chat.id, address, network, now_ts),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Siguiendo {address} en {network}")


async def remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /remove <direccion> <red>")
        return

    address, network = context.args[0].lower(), context.args[1].lower()
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM wallets WHERE chat_id=? AND address=? AND network=?",
        (update.effective_chat.id, address, network),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑️ Dejaste de seguir {address} en {network}")


async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT address, network FROM wallets WHERE chat_id=?",
        (update.effective_chat.id,),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No estás siguiendo ninguna wallet todavía. Usa /add para empezar.")
        return

    text = "📋 Wallets que sigues:\n" + "\n".join(f"- {a} ({n})" for a, n in rows)
    await update.message.reply_text(text)


# ----------------------------------------------------------------------------
# Lógica de consulta de actividad (polling a Etherscan V2)
# ----------------------------------------------------------------------------

def fetch_activity(address: str, network: str, since_ts: int):
    """Devuelve una lista de eventos nuevos (tx normales + transferencias de tokens).

    Funciona tanto con la API de Etherscan (multi-cadena) como con la API
    Etherscan-compatible que expone cualquier explorador Blockscout, como el
    de Robinhood Chain.
    """
    chain = CHAINS[network]
    events = []

    if chain["kind"] == "etherscan":
        base_url = "https://api.etherscan.io/v2/api"
        extra_params = {"chainid": chain["chainid"], "apikey": ETHERSCAN_API_KEY}
    else:  # blockscout
        base_url = chain["api_base"]
        extra_params = {}  # la API pública de Blockscout no exige clave para uso ligero

    for action in ("txlist", "tokentx"):
        params = {
            "module": "account",
            "action": action,
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "sort": "desc",
            **extra_params,
        }
        try:
            r = requests.get(base_url, params=params, timeout=15)
            data = r.json()
            if data.get("status") == "1":
                for tx in data["result"][:20]:
                    ts = int(tx["timeStamp"])
                    if ts > since_ts:
                        events.append({**tx, "_kind": "token" if action == "tokentx" else "normal", "_ts": ts})
        except Exception as e:
            log.warning("Error consultando %s (%s) para %s: %s", action, network, address, e)

    events.sort(key=lambda e: e["_ts"])
    return events


def format_event(e: dict, address: str, network: str) -> str:
    explorer = CHAINS[network]["explorer"]
    direction = "📥 Recibido" if e["to"].lower() == address.lower() else "📤 Enviado"

    if e["_kind"] == "token":
        decimals = int(e.get("tokenDecimal", 18) or 18)
        value = int(e["value"]) / (10 ** decimals)
        symbol = e.get("tokenSymbol", "TOKEN")
        detail = f"{direction} {value:.4f} {symbol}"
    else:
        value = int(e["value"]) / 1e18
        detail = f"{direction} {value:.5f} (moneda nativa)"

    return (
        f"{detail}\n"
        f"De: {e['from']}\n"
        f"A: {e['to']}\n"
        f"https://{explorer}/tx/{e['hash']}"
    )


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, address, network, last_seen_ts FROM wallets")
    rows = c.fetchall()

    for chat_id, address, network, last_seen_ts in rows:
        events = fetch_activity(address, network, last_seen_ts)

        if not events:
            continue

        for e in events:
            msg = f"🔔 Actividad en {address} ({network}):\n\n" + format_event(e, address, network)
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, disable_web_page_preview=True)
            except Exception as ex:
                log.warning("No se pudo enviar mensaje a %s: %s", chat_id, ex)

        new_ts = max(e["_ts"] for e in events)
        c2 = conn.cursor()
        c2.execute(
            "UPDATE wallets SET last_seen_ts=? WHERE chat_id=? AND address=? AND network=?",
            (new_ts, chat_id, address, network),
        )
        conn.commit()

    conn.close()


# ----------------------------------------------------------------------------
# Arranque
# ----------------------------------------------------------------------------

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("redes", redes))
    app.add_handler(CommandHandler("add", add_wallet))
    app.add_handler(CommandHandler("remove", remove_wallet))
    app.add_handler(CommandHandler("list", list_wallets))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL, first=10)

    log.info("Bot iniciado, revisando wallets cada %s segundos", POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
