"""
notificador_telegram.py — aviso por Telegram cada vez que un agente
analiza una empresa.

Uso la API HTTP de Telegram directamente con requests en vez de la
librería python-telegram-bot: para mandar un mensaje de texto no
necesito un framework entero de bots con asyncio — es un POST a una
URL y ya está. Menos dependencias, menos superficie de fallo.

Configuración (variables de entorno):
  TELEGRAM_BOT_TOKEN — lo da @BotFather al crear el bot
  TELEGRAM_CHAT_ID   — tu chat id (escríbele algo al bot y consulta
                       https://api.telegram.org/bot<TOKEN>/getUpdates
                       para verlo)

Diseño deliberado: si Telegram no está configurado o falla, el
notificador AVISA en el log pero nunca lanza excepción. Un análisis
que costó llamadas a Groq/Gemini no se puede perder porque el aviso
de cortesía no salió.
"""

import os
import logging

import requests
from dotenv import load_dotenv

# Cargo el .env de la carpeta si existe — así las claves no dependen de
# pegarlas a mano en cada sesión nueva de PowerShell.
load_dotenv()

log = logging.getLogger(__name__)

# Emojis por veredicto para que el mensaje se lea de un vistazo
# en el móvil sin abrir nada.
EMOJI_VEREDICTO = {
    "MUY_INTERESANTE": "\U0001F7E2",   # circulo verde
    "INTERESANTE": "\U0001F7E1",       # circulo amarillo
    "NADA_INTERESANTE": "\U0001F534",  # circulo rojo
    "ALUCINACION": "\U0001F6AB",       # prohibido
    # Los nombres antiguos, por si quedan filas viejas sin migrar
    "SEGURO": "\U0001F7E2", "DUDOSO": "\U0001F7E1",
    "HUMO": "\U0001F534", "DESCARTADO_POR_ALUCINACION": "\U0001F6AB",
}


def enviar_telegram(texto: str) -> bool:
    """
    Manda un mensaje al chat configurado. Devuelve True si salió.
    Si faltan las variables de entorno, lo digo una vez en el log y
    sigo — el pipeline funciona igual sin Telegram.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.info("Telegram sin configurar (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — no envío aviso")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Telegram devolvió {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.warning(f"No pude enviar el aviso de Telegram: {e}")
        return False


def notificar_analisis(agente: str, ticker: str, nombre: str, modelo: str,
                       veredicto: str, citas_verificadas: int = None,
                       citas_totales: int = None, extra: str = None) -> bool:
    """
    Formatea y envía el aviso estándar de "acabo de analizar una empresa".
    Lo llaman detective.py y auditor.py justo después de guardar el
    resultado en la base de datos — nunca antes, para no avisar de un
    análisis que luego falló al persistirse.
    """
    emoji = EMOJI_VEREDICTO.get(veredicto, "❓")

    lineas = [
        f"{emoji} <b>{ticker}</b> — {nombre or 'sin nombre'}",
        f"Agente: {agente} ({modelo})",
        f"Veredicto: <b>{veredicto}</b>",
    ]
    if citas_totales:
        lineas.append(f"Citas verificadas: {citas_verificadas}/{citas_totales}")
    if extra:
        lineas.append(extra)

    return enviar_telegram("\n".join(lineas))
