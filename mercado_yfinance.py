"""
mercado_yfinance.py — descargo datos de mercado para cada empresa

La SEC no me da market cap, precio actual, ni rango de 52 semanas — eso
solo lo tiene el mercado en tiempo real. Sin estos datos no puedo aplicar
el filtro de tamaño de la Capa 1 ni calcular el score de posición en 52
semanas de la Capa 2, que es el predictor más fuerte según la
investigación académica que revisamos.

yfinance no es la API oficial de Yahoo Finance — es una librería que lee
endpoints no documentados. Puede fallar o devolver datos vacíos sin previo
aviso, así que trato cada ticker con cautela: reintentos, timeouts, y
nunca dejo que un ticker roto tumbe el proceso completo.

Cómo usarlo:
  # Todas las empresas activas sin datos de mercado o con datos viejos:
  python mercado_yfinance.py

  # Solo un ticker concreto, para depurar:
  python mercado_yfinance.py --ticker AAPL

  # Limita cuántas empresas procesa en esta ejecución:
  python mercado_yfinance.py --limite 200
"""

import os
import time
import logging
import argparse
from typing import Optional

import yfinance as yf
import psycopg2
from dotenv import load_dotenv

# Cargo el .env de la carpeta si existe — así las claves no dependen de
# pegarlas a mano en cada sesión nueva de PowerShell.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# yfinance no documenta ningún límite oficial, pero si lo machaco sin
# pausas empieza a devolver respuestas vacías o bloquea temporalmente
# la IP. Subí esto de 0.3 a 0.6s tras ver bloqueos reales con miles de
# tickers seguidos — más lento pero fiable.
PAUSA_ENTRE_TICKERS = 0.6


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def obtener_datos_mercado(ticker: str) -> Optional[dict]:
    """
    Descarga market cap, precio, rango de 52 semanas, volumen medio y
    bolsa desde yfinance. Devuelvo None si el ticker no existe o no
    tiene ninguno de los datos clave — no es un error de red, es que
    la empresa probablemente ya no cotiza o cambió de ticker.
    """
    for intento in range(3):
        try:
            t = yf.Ticker(ticker)
            info = t.info

            # currentPrice a veces no está (mercado cerrado, ticker poco
            # líquido); regularMarketPrice es el fallback que casi
            # siempre existe si el ticker es válido.
            market_cap = info.get("marketCap")
            precio = info.get("currentPrice") or info.get("regularMarketPrice")
            max_52w = info.get("fiftyTwoWeekHigh")
            min_52w = info.get("fiftyTwoWeekLow")
            volumen = info.get("averageVolume")
            bolsa = info.get("fullExchangeName") or info.get("exchange")

            # Short interest, en la misma llamada — no cuesta una request
            # extra. sharesShort y shortPercentOfFloat vienen del reporte
            # quincenal de FINRA que Yahoo republica; dateShortInterest es
            # epoch en segundos. Cualquiera puede faltar en tickers poco
            # cubiertos — None es un valor honesto, no un error.
            shares_short = info.get("sharesShort")
            short_pct = info.get("shortPercentOfFloat")
            fecha_short = None
            epoch_short = info.get("dateShortInterest")
            if epoch_short:
                try:
                    from datetime import datetime as _dt
                    fecha_short = _dt.fromtimestamp(epoch_short).date()
                except (ValueError, OSError, OverflowError):
                    fecha_short = None

            # Si no tengo ni market cap ni precio, no hay nada que
            # guardar — probablemente deslistada o ticker incorrecto.
            if market_cap is None and precio is None:
                return None

            posicion_52w = None
            if precio is not None and max_52w and min_52w and max_52w > min_52w:
                posicion_52w = round((precio - min_52w) / (max_52w - min_52w), 4)

            return {
                "market_cap_usd":    market_cap,
                "precio_actual":     precio,
                "precio_min_52w":    min_52w,
                "precio_max_52w":    max_52w,
                "posicion_52w":      posicion_52w,
                "volumen_medio_30d": volumen,
                "bolsa":             bolsa,
                "shares_short":         shares_short,
                "short_pct_float":      short_pct,
                "fecha_short_interest": fecha_short,
            }

        except Exception as e:
            texto_error = str(e)

            # Un 404 "Quote not found" es definitivo — el ticker no existe
            # en Yahoo, no hay ninguna razón para reintentar 3 veces algo
            # que no va a cambiar. Reintentar aquí solo desperdicia tiempo
            # en cada uno de los cientos de tickers deslistados que hay
            # en una base de datos con 10 años de histórico.
            if "404" in texto_error or "Quote not found" in texto_error:
                log.info(f"{ticker}: no existe en Yahoo Finance (404) — descarto sin reintentar")
                return None

            log.warning(f"Intento {intento+1}/3 fallido para {ticker}: {e}")
            time.sleep(2 * (intento + 1))

    return None


def guardar_datos_mercado(conn, empresa_id: int, datos: dict) -> bool:
    """Actualizo la fila de la empresa con el snapshot de mercado recién descargado."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            update empresas set
                market_cap_usd    = %s,
                precio_actual     = %s,
                precio_min_52w    = %s,
                precio_max_52w    = %s,
                posicion_52w      = %s,
                volumen_medio_30d = %s,
                bolsa             = %s,
                shares_short         = %s,
                short_pct_float      = %s,
                fecha_short_interest = %s,
                ultimo_update     = now()
            where id = %s
            """,
            (
                datos.get("market_cap_usd"),
                datos.get("precio_actual"),
                datos.get("precio_min_52w"),
                datos.get("precio_max_52w"),
                datos.get("posicion_52w"),
                datos.get("volumen_medio_30d"),
                datos.get("bolsa"),
                datos.get("shares_short"),
                datos.get("short_pct_float"),
                datos.get("fecha_short_interest"),
                empresa_id,
            )
        )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar datos de mercado para empresa {empresa_id}: {e}")
        return False
    finally:
        cur.close()


def obtener_empresas_pendientes(conn, limite: Optional[int] = None) -> list:
    """
    Empresas activas sin market cap todavía, o con datos de hace más
    de 24 horas. El precio cambia cada día, así que no basta con
    comprobar si el dato existe — también importa si está fresco.

    Dos decisiones aquí:
    - Consulto la tabla empresas, no la vista empresas_activas: las
      vistas con select * congelan sus columnas al crearse y ya me
      costó una tarde con la columna 'bolsa'.
    - Filtro solo por activa=true, sin excluir estado='descartada'.
      Una empresa descartada por la Capa 1 (p.ej. por tamaño) necesita
      seguir recibiendo precios frescos: si crece hasta entrar en rango,
      quiero que la próxima pasada de la Capa 1 la vea con datos de hoy,
      no con el snapshot congelado del día que la descartó.
    """
    cur = conn.cursor()
    try:
        query = """
            select id, ticker
            from empresas
            where activa = true
              and (market_cap_usd is null
                   or ultimo_update < now() - interval '24 hours')
            order by primera_deteccion desc
        """
        if limite:
            query += f" limit {limite}"
        cur.execute(query)
        return cur.fetchall()
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(
        description="Descarga datos de mercado (yfinance) para las empresas"
    )
    parser.add_argument("--ticker", type=str, help="Procesa solo este ticker (para depurar)")
    parser.add_argument("--limite", type=int, help="Máximo de empresas a procesar")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        if args.ticker:
            cur = conn.cursor()
            cur.execute("select id, ticker from empresas where ticker = %s", (args.ticker,))
            empresas = cur.fetchall()
            cur.close()
            if not empresas:
                log.error(f"Ticker {args.ticker} no encontrado en la base de datos")
                return
        else:
            empresas = obtener_empresas_pendientes(conn, args.limite)

        log.info(f"Empresas a procesar: {len(empresas)}")

        ok, sin_datos, error = 0, 0, 0

        for i, (empresa_id, ticker) in enumerate(empresas, 1):
            if not ticker or not ticker.strip():
                sin_datos += 1
                continue

            try:
                datos = obtener_datos_mercado(ticker.strip())
                if datos:
                    if guardar_datos_mercado(conn, empresa_id, datos):
                        ok += 1
                    else:
                        error += 1
                else:
                    sin_datos += 1
            except Exception as e:
                log.error(f"Error inesperado con {ticker}: {e}")
                error += 1

            if i % 100 == 0:
                log.info(f"Progreso: {i}/{len(empresas)} — ok: {ok} | sin datos: {sin_datos} | error: {error}")

            time.sleep(PAUSA_ENTRE_TICKERS)

        log.info(
            f"\nResumen final:\n"
            f"  Correctas:   {ok}\n"
            f"  Sin datos:   {sin_datos}\n"
            f"  Errores:     {error}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
