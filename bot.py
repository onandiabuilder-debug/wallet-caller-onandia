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
BLOCKSCOUT_API_KEY = os.environ.get("BLOCKSCOUT_API_KEY", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))

# Umbrales para detectar "confluencias": varias wallets seguidas comprando el mismo token.
CONFLUENCE_MIN_WALLETS = int(os.environ.get("CONFLUENCE_MIN_WALLETS", "5"))
CONFLUENCE_MIN_USD = float(os.environ.get("CONFLUENCE_MIN_USD", "500"))
CONFLUENCE_WINDOW_HOURS = float(os.environ.get("CONFLUENCE_WINDOW_HOURS", "24"))

# Mapeo de nuestros nombres de red a los "chainId" que usa DexScreener (API gratuita,
# sin clave, para estimar el precio en USD de un token en el momento de la compra).
DEXSCREENER_CHAIN_IDS = {
    "ethereum": "ethereum",
    "bsc": "bsc",
    "polygon": "polygon",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "base": "base",
    "avalanche": "avalanche",
    # "robinhood" se omite: es una red demasiado nueva y todavía no está indexada en DexScreener.
}

# Redes soportadas. Cada una indica qué tipo de API usar:
# - "etherscan": API V2 unificada de Etherscan (necesita ETHERSCAN_API_KEY)
# - "blockscout_pro": PRO API multi-cadena de Blockscout (necesita BLOCKSCOUT_API_KEY,
#   gratuita en dev.blockscout.com). Se usa para Robinhood Chain, que Etherscan no soporta
#   y cuyo acceso directo al explorador sin clave está siendo descontinuado.
CHAINS = {
    "ethereum":  {"kind": "etherscan", "chainid": 1,     "explorer": "etherscan.io"},
    "bsc":       {"kind": "etherscan", "chainid": 56,    "explorer": "bscscan.com"},
    "polygon":   {"kind": "etherscan", "chainid": 137,   "explorer": "polygonscan.com"},
    "arbitrum":  {"kind": "etherscan", "chainid": 42161, "explorer": "arbiscan.io"},
    "optimism":  {"kind": "etherscan", "chainid": 10,    "explorer": "optimistic.etherscan.io"},
    "base":      {"kind": "etherscan", "chainid": 8453,  "explorer": "basescan.org"},
    "avalanche": {"kind": "etherscan", "chainid": 43114, "explorer": "snowtrace.io"},
    "robinhood": {
        "kind": "blockscout_pro",
        "chain_id": 4663,
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
            label TEXT DEFAULT NULL,
            PRIMARY KEY (chat_id, address, network)
        )"""
    )
    # Si la tabla ya existía de una versión anterior sin la columna 'label',
    # la añadimos ahora (ignoramos el error si ya existe).
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN label TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    # Historial de "compras" (tokens recibidos) detectadas, con su valor estimado en USD,
    # para poder buscar confluencias entre varias wallets seguidas.
    c.execute(
        """CREATE TABLE IF NOT EXISTS purchases (
            chat_id INTEGER,
            network TEXT,
            token_contract TEXT,
            token_symbol TEXT,
            address TEXT,
            usd_value REAL,
            tx_hash TEXT,
            ts INTEGER,
            PRIMARY KEY (network, tx_hash, address, token_contract)
        )"""
    )

    # Recuerda cuántas wallets distintas ya generaron una alerta de confluencia para un
    # token, para no repetir el mismo aviso en cada ciclo de comprobación.
    c.execute(
        """CREATE TABLE IF NOT EXISTS confluence_alerts (
            chat_id INTEGER,
            network TEXT,
            token_contract TEXT,
            notified_count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, network, token_contract)
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
        "/add <direccion> [red] [etiqueta] — Empieza a seguir una wallet. Si no indicas red, la detecto sola.\n"
        "/label <direccion> <red> <etiqueta> — Pon o cambia la etiqueta de una wallet\n"
        "/remove <direccion> <red> — Deja de seguir una wallet\n"
        "/list — Muestra las wallets que sigues\n"
        "/redes — Muestra las redes soportadas\n\n"
        "Ejemplos:\n"
        "/add 0xabc123...\n"
        "/add 0xabc123... Wallet de Juan\n"
        "/add 0xabc123... ethereum\n"
        "/add 0xabc123... ethereum Wallet de Juan\n"
        "/label 0xabc123... ethereum Ahorros"
    )
    await update.message.reply_text(text)


async def redes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Redes soportadas: " + ", ".join(CHAINS.keys()))


async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Uso: /add <direccion> [red] [etiqueta]\n"
            "Ej: /add 0xabc... — detecto la red solo\n"
            "Ej: /add 0xabc... ethereum Wallet de Juan — la indicas tú"
        )
        return

    address = context.args[0].lower()
    rest = context.args[1:]

    if not is_valid_address(address):
        await update.message.reply_text(
            "Esa dirección no parece válida (debe empezar por 0x y tener 42 caracteres)."
        )
        return

    # Si la segunda palabra coincide con una red soportada, se usa esa red directamente.
    # Si no, se trata todo como etiqueta y se intenta detectar la red automáticamente.
    if rest and rest[0].lower() in CHAINS:
        network = rest[0].lower()
        label = " ".join(rest[1:]).strip() or None
        await _add_single_wallet(update, address, network, label)
        return

    label = " ".join(rest).strip() or None
    await update.message.reply_text("🔍 No indicaste red, voy a comprobar en cuáles tiene actividad esta wallet...")

    detected = detect_networks(address)

    if not detected:
        await update.message.reply_text(
            "No encontré actividad en ninguna red soportada para esa dirección. "
            "Puede que sea una wallet nueva sin movimientos todavía, o que esté en una red "
            "que no soporto. Si sabes la red, indícala directamente: /add <direccion> <red>"
        )
        return

    for network in detected:
        await _add_single_wallet(update, address, network, label, silent=True)

    redes_txt = ", ".join(detected)
    if label:
        await update.message.reply_text(f"✅ Detecté actividad en: {redes_txt}. Siguiendo {address} como «{label}» en esas redes.")
    else:
        await update.message.reply_text(f"✅ Detecté actividad en: {redes_txt}. Siguiendo {address} en esas redes.")


async def _add_single_wallet(update: Update, address: str, network: str, label: str, silent: bool = False):
    """Inserta (o actualiza la etiqueta de) una wallet ya sabiendo su red."""
    if CHAINS[network]["kind"] == "blockscout_pro" and not BLOCKSCOUT_API_KEY:
        await update.message.reply_text(
            "⚠️ Para seguir wallets en robinhood necesito la variable BLOCKSCOUT_API_KEY "
            "configurada en Railway (clave gratuita en dev.blockscout.com). "
            "Te sigo dejando añadirla, pero no recibirás avisos hasta que la configures."
        )

    conn = get_conn()
    c = conn.cursor()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    c.execute(
        "INSERT OR IGNORE INTO wallets (chat_id, address, network, last_seen_ts, label) VALUES (?, ?, ?, ?, ?)",
        (update.effective_chat.id, address, network, now_ts, label),
    )
    if label:
        c.execute(
            "UPDATE wallets SET label=? WHERE chat_id=? AND address=? AND network=?",
            (label, update.effective_chat.id, address, network),
        )
    conn.commit()
    conn.close()

    if not silent:
        if label:
            await update.message.reply_text(f"✅ Siguiendo {address} en {network} como «{label}»")
        else:
            await update.message.reply_text(f"✅ Siguiendo {address} en {network}")


async def label_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Uso: /label <direccion> <red> <etiqueta>\nEj: /label 0xabc... ethereum Ahorros"
        )
        return

    address, network = context.args[0].lower(), context.args[1].lower()
    label = " ".join(context.args[2:]).strip()

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE wallets SET label=? WHERE chat_id=? AND address=? AND network=?",
        (label, update.effective_chat.id, address, network),
    )
    updated = c.rowcount
    conn.commit()
    conn.close()

    if updated:
        await update.message.reply_text(f"🏷️ Etiqueta actualizada: {address} ({network}) → «{label}»")
    else:
        await update.message.reply_text(
            "No encontré esa wallet en esa red entre las que sigues. Usa /list para verlas."
        )


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
        "SELECT address, network, label FROM wallets WHERE chat_id=?",
        (update.effective_chat.id,),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No estás siguiendo ninguna wallet todavía. Usa /add para empezar.")
        return

    lines = []
    for address, network, label in rows:
        if label:
            lines.append(f"- {address} ({network}) — «{label}»")
        else:
            lines.append(f"- {address} ({network})")

    text = "📋 Wallets que sigues:\n" + "\n".join(lines)
    await update.message.reply_text(text)


# ----------------------------------------------------------------------------
# Lógica de consulta de actividad (polling a Etherscan V2)
# ----------------------------------------------------------------------------

def detect_networks(address: str):
    """Comprueba en todas las redes soportadas si esta dirección tiene alguna
    actividad (al menos una transacción alguna vez) y devuelve la lista de
    redes donde se encontró algo."""
    detected = []
    for network, chain in CHAINS.items():
        if chain["kind"] == "blockscout_pro" and not BLOCKSCOUT_API_KEY:
            continue  # no podemos comprobar esta red sin la clave configurada
        try:
            events = fetch_activity(address, network, since_ts=0)
        except Exception as e:
            log.warning("Error detectando actividad en %s para %s: %s", network, address, e)
            events = []
        if events:
            detected.append(network)
    return detected


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
    else:  # blockscout_pro
        base_url = "https://api.blockscout.com/v2/api"
        extra_params = {"chain_id": chain["chain_id"], "apikey": BLOCKSCOUT_API_KEY}

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


# ----------------------------------------------------------------------------
# Precio en USD (DexScreener, gratis y sin clave) y detección de confluencias
# ----------------------------------------------------------------------------

def get_token_price_usd(token_contract: str, network: str):
    """Precio aproximado actual del token en USD, según el par con más liquidez
    en DexScreener. Devuelve None si no se encuentra o la red no está soportada."""
    chain_id = DEXSCREENER_CHAIN_IDS.get(network)
    if not chain_id:
        return None
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_contract}", timeout=10
        )
        data = r.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain_id]
        if not pairs:
            return None
        # Nos quedamos con el par de mayor liquidez, suele ser el más fiable.
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        price = best.get("priceUsd")
        return float(price) if price is not None else None
    except Exception as e:
        log.warning("Error consultando precio de %s en %s: %s", token_contract, network, e)
        return None


def record_purchase_and_check_confluence(conn, chat_id: int, address: str, network: str, e: dict):
    """Si el evento es una compra de token (token recibido), estima su valor en
    USD, la guarda, y comprueba si eso dispara una alerta de confluencia.
    Devuelve el texto de la alerta de confluencia, o None si no hay ninguna."""
    if e["_kind"] != "token" or e["to"].lower() != address.lower():
        return None  # solo nos interesan tokens RECIBIDOS (compras), no enviados

    token_contract = e.get("contractAddress", "").lower()
    if not token_contract:
        return None

    decimals = int(e.get("tokenDecimal", 18) or 18)
    amount = int(e["value"]) / (10 ** decimals)
    symbol = e.get("tokenSymbol", "TOKEN")

    price = get_token_price_usd(token_contract, network)
    usd_value = amount * price if price is not None else None

    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO purchases "
        "(chat_id, network, token_contract, token_symbol, address, usd_value, tx_hash, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, network, token_contract, symbol, address.lower(), usd_value, e["hash"], e["_ts"]),
    )
    conn.commit()

    if usd_value is None or usd_value < CONFLUENCE_MIN_USD:
        return None

    # ¿Cuántas wallets DISTINTAS de este chat han comprado >= umbral de este mismo
    # token en la ventana de tiempo configurada?
    window_start = e["_ts"] - int(CONFLUENCE_WINDOW_HOURS * 3600)
    c.execute(
        "SELECT DISTINCT address FROM purchases "
        "WHERE chat_id=? AND network=? AND token_contract=? AND usd_value>=? AND ts>=?",
        (chat_id, network, token_contract, CONFLUENCE_MIN_USD, window_start),
    )
    wallets_involved = [row[0] for row in c.fetchall()]
    count = len(wallets_involved)

    if count < CONFLUENCE_MIN_WALLETS:
        return None

    c.execute(
        "SELECT notified_count FROM confluence_alerts WHERE chat_id=? AND network=? AND token_contract=?",
        (chat_id, network, token_contract),
    )
    row = c.fetchone()
    already_notified = row[0] if row else 0

    if count <= already_notified:
        return None  # ya avisamos de esta cifra (o una mayor) antes

    c.execute(
        "INSERT INTO confluence_alerts (chat_id, network, token_contract, notified_count) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id, network, token_contract) DO UPDATE SET notified_count=excluded.notified_count",
        (chat_id, network, token_contract, count),
    )
    conn.commit()

    # Etiquetas de las wallets implicadas, si las tienen, para que el aviso sea legible.
    placeholders = ",".join("?" for _ in wallets_involved)
    c.execute(
        f"SELECT address, label FROM wallets WHERE chat_id=? AND network=? AND address IN ({placeholders})",
        (chat_id, network, *wallets_involved),
    )
    label_map = {addr: lbl for addr, lbl in c.fetchall()}
    wallet_lines = "\n".join(
        f"  • {label_map.get(w) or w} ({w})" if label_map.get(w) else f"  • {w}"
        for w in wallets_involved
    )

    return (
        f"🔥 Confluencia detectada en {symbol} ({network})\n\n"
        f"{count} wallets que sigues han comprado más de {CONFLUENCE_MIN_USD:.0f}$ "
        f"en las últimas {CONFLUENCE_WINDOW_HOURS:.0f}h:\n{wallet_lines}\n\n"
        f"Contrato: {token_contract}"
    )


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, address, network, last_seen_ts, label FROM wallets")
    rows = c.fetchall()

    for chat_id, address, network, last_seen_ts, label in rows:
        events = fetch_activity(address, network, last_seen_ts)

        if not events:
            continue

        wallet_display = f"«{label}» ({address}, {network})" if label else f"{address} ({network})"

        for e in events:
            msg = f"🔔 Actividad en {wallet_display}:\n\n" + format_event(e, address, network)
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, disable_web_page_preview=True)
            except Exception as ex:
                log.warning("No se pudo enviar mensaje a %s: %s", chat_id, ex)

            confluence_msg = record_purchase_and_check_confluence(conn, chat_id, address, network, e)
            if confluence_msg:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=confluence_msg, disable_web_page_preview=True)
                except Exception as ex:
                    log.warning("No se pudo enviar aviso de confluencia a %s: %s", chat_id, ex)

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
    app.add_handler(CommandHandler("label", label_wallet))
    app.add_handler(CommandHandler("remove", remove_wallet))
    app.add_handler(CommandHandler("list", list_wallets))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL, first=10)

    log.info("Bot iniciado, revisando wallets cada %s segundos", POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
