# Bot de Telegram para seguir wallets EVM

Este bot te avisa por Telegram cuando una wallet que le indiques hace algo:
recibe o envía ETH/BNB/MATIC (o la moneda nativa de la red) o tokens.

No necesitas saber programar para ponerlo en marcha. Sigue los pasos de la guía
que te he dado en la conversación (crear el bot con BotFather, conseguir la
clave de Etherscan, subir el código a GitHub y desplegarlo en Railway).

## Archivos incluidos

- `bot.py` — el código del bot (no hace falta tocarlo)
- `requirements.txt` — lista de librerías que necesita
- `.env.example` — plantilla de las claves que hay que configurar
- `Procfile` — le dice a Railway cómo arrancar el bot

## Comandos del bot una vez esté funcionando

- `/start` — muestra la ayuda
- `/redes` — lista las redes soportadas
- `/add <direccion> [red] [etiqueta]` — empieza a seguir una wallet. Si no indicas la red, el bot comprueba automáticamente en cuáles de las redes soportadas tiene actividad esa dirección y la sigue ahí. Ejemplos:
  - `/add 0xabc123...` (detecta la red sola)
  - `/add 0xabc123... Wallet de Juan` (detecta la red, y le pone esa etiqueta)
  - `/add 0xabc123... ethereum` (red indicada a mano)
  - `/add 0xabc123... ethereum Wallet de Juan` (red y etiqueta indicadas a mano)
- `/label <direccion> <red> <etiqueta>` — pone o cambia la etiqueta de una wallet que ya sigues, ej: `/label 0xabc123... ethereum Ahorros`
- `/remove <direccion> <red>` — deja de seguir una wallet
- `/list` — muestra las wallets que sigues, con su etiqueta si tiene

## Redes soportadas

ethereum, bsc, polygon, arbitrum, optimism, base, avalanche, robinhood

La red `robinhood` es Robinhood Chain (la L2 de Robinhood). Etherscan no la
soporta, así que el bot usa la PRO API oficial de Blockscout, que requiere su
propia clave gratuita — ver más abajo.

## Claves necesarias

- `TELEGRAM_BOT_TOKEN` — de @BotFather en Telegram
- `ETHERSCAN_API_KEY` — de etherscan.io, cubre ethereum/bsc/polygon/arbitrum/optimism/base/avalanche
- `BLOCKSCOUT_API_KEY` — **necesaria solo si vas a seguir wallets en la red `robinhood`**.
  Sácala gratis en [dev.blockscout.com](https://dev.blockscout.com) (regístrate y genera
  una API key; el plan gratuito incluye 5 peticiones por segundo, de sobra para uso
  personal). Sin esta clave, las wallets en `robinhood` no recibirán avisos.

## Notas

- El bot revisa las wallets cada 2 minutos (puedes cambiarlo con la variable
  `POLL_INTERVAL_SECONDS`).
- Cada wallet se puede seguir en varias redes a la vez, solo repite `/add` con
  la red que quieras.
- Los datos de qué wallets sigues se guardan en un archivo `wallets.db` dentro
  del propio servicio de Railway.

## Detección de confluencias

Además de avisarte de la actividad de cada wallet por separado, el bot vigila
si **varias de las wallets que sigues compran el mismo token**. Si detecta que
5 o más wallets distintas (configurable) han comprado más de 500$ (configurable)
del mismo token en las últimas 24h (configurable), te manda un aviso especial
con el listado de wallets implicadas.

El valor en USD de cada compra se estima con la API pública y gratuita de
DexScreener, así que no necesita ninguna clave adicional. Esto no funciona
todavía en la red `robinhood` porque es demasiado nueva y aún no está indexada
en DexScreener — si en el futuro lo está, solo hay que añadirla al diccionario
`DEXSCREENER_CHAIN_IDS` del código.

Variables para ajustar la sensibilidad (todas opcionales, con estos valores
por defecto):

- `CONFLUENCE_MIN_WALLETS=5` — nº mínimo de wallets distintas
- `CONFLUENCE_MIN_USD=500` — valor mínimo de cada compra individual
- `CONFLUENCE_WINDOW_HOURS=24` — ventana de tiempo a considerar
