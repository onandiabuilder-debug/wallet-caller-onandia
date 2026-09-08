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
- `/add <direccion> <red>` — empieza a seguir una wallet, ej: `/add 0xabc123... ethereum`
- `/remove <direccion> <red>` — deja de seguir una wallet
- `/list` — muestra las wallets que sigues

## Redes soportadas

ethereum, bsc, polygon, arbitrum, optimism, base, avalanche, robinhood

La red `robinhood` es Robinhood Chain (la L2 de Robinhood), y a diferencia de
las demás no usa Etherscan: el bot consulta directamente el explorador
Blockscout oficial de esa red, así que no necesita ninguna clave adicional.

## Notas

- El bot revisa las wallets cada 2 minutos (puedes cambiarlo con la variable
  `POLL_INTERVAL_SECONDS`).
- Cada wallet se puede seguir en varias redes a la vez, solo repite `/add` con
  la red que quieras.
- Los datos de qué wallets sigues se guardan en un archivo `wallets.db` dentro
  del propio servicio de Railway.
