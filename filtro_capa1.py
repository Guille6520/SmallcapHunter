"""
filtro_capa1.py — filtros binarios de descarte rápido

Antes de gastar ni un token de LLM en analizar una empresa, la paso por
estos cuatro filtros. Si falla cualquiera, la descarto directamente y
guardo el motivo — así puedo revisar después por qué el sistema tomó
cada decisión, no solo cuáles pasaron.

Los cuatro filtros:
  1. Market cap entre 50M y 2B (parámetros en la tabla configuracion)
  2. Bolsa válida — NYSE/NASDAQ/AMEX, descarto OTC pink sheets
  3. Cluster buying — 3+ insiders distintos comprando en una ventana
     de 60 días (no en cualquier momento de la historia, en una
     ventana concreta — eso es lo que hace la señal fuerte)
  4. Al menos un comprador DEL CLUSTER es C-suite (CEO, CFO, COO,
     President, Chairman) — un director independiente comprando pesa menos

El quinto filtro que teníamos diseñado (descartar compras cosméticas
justo antes de una ampliación de capital) lo dejo pendiente a propósito:
necesita datos de 8-K que todavía no ingiero. Lo documento como
trabajo futuro en vez de fingir que ya está resuelto.

Cómo usarlo:
  # Todas las empresas activas con datos de mercado:
  python filtro_capa1.py

  # Solo una empresa, para depurar:
  python filtro_capa1.py --ticker AAPL
"""

import os
import re
import logging
import argparse
from datetime import timedelta

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
    # 127.0.0.1 y no "localhost": con Docker Desktop en Windows, localhost
    # puede resolver mal y la conexión falla de forma intermitente.
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Palabras que busco en el cargo para considerar a alguien C-suite.
# Un director independiente ("Director") no cuenta — necesito que sea
# alguien con acceso directo a la operación diaria de la empresa.
PALABRAS_CSUITE = [
    "CHIEF EXECUTIVE", "CEO",
    "CHIEF FINANCIAL", "CFO",
    "CHIEF OPERATING", "COO",
    "PRESIDENT", "CHAIRMAN",
]

# Bolsas que acepto. Cualquier cosa con "OTC" o "PINK" la descarto —
# ahí la liquidez y la calidad de la información son demasiado bajas
# para fiarme sin verificación adicional.
BOLSAS_VALIDAS = ["NASDAQ", "NYSE", "AMEX"]
BOLSAS_DESCARTADAS = ["OTC", "PINK", "GREY", "EXPERT MARKET"]


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def leer_configuracion(conn) -> dict:
    """Leo los parámetros de la tabla configuracion en vez de tenerlos
    hardcodeados — así puedo ajustar los umbrales sin tocar código."""
    cur = conn.cursor()
    try:
        cur.execute("select clave, valor from configuracion")
        return {clave: valor for clave, valor in cur.fetchall()}
    finally:
        cur.close()


def bolsa_es_valida(bolsa: str) -> bool:
    """
    Compruebo primero si contiene alguna palabra de descarte (OTC, Pink)
    antes que las válidas, porque nombres como 'OTC Markets - NASDAQ
    Referenced' podrían dar falso positivo si mirara solo la lista
    de válidas.
    """
    if not bolsa:
        return False
    bolsa_mayus = bolsa.upper()
    if any(descartada in bolsa_mayus for descartada in BOLSAS_DESCARTADAS):
        return False
    return any(valida in bolsa_mayus for valida in BOLSAS_VALIDAS)


def es_cargo_csuite(cargo: str) -> bool:
    """
    Mismo patrón que en bolsa_es_valida: quito primero lo que puede dar
    falso positivo, y solo después busco las palabras buenas.

    El caso que me mordió: "PRESIDENT" está dentro de "VICE PRESIDENT",
    así que cualquier VP de ventas pasaba como C-suite por pura
    coincidencia de substring. Elimino las variantes de vicepresidente
    (la forma larga y las siglas VP/SVP/EVP/AVP) antes de comparar.
    "Vice Chairman" tampoco es el chairman, así que recibe el mismo trato.
    """
    if not cargo:
        return False
    cargo_limpio = cargo.upper()
    cargo_limpio = cargo_limpio.replace("VICE PRESIDENT", "")
    cargo_limpio = cargo_limpio.replace("VICE CHAIRMAN", "")
    cargo_limpio = re.sub(r"\b[SEA]?VP\b", "", cargo_limpio)
    return any(palabra in cargo_limpio for palabra in PALABRAS_CSUITE)


def detectar_cluster_buying(transacciones: list, dias_ventana: int, min_insiders: int) -> dict:
    """
    Recibo una lista de (fecha, nombre_insider) de compras P, ordenada
    por fecha. Busco si existe alguna ventana de `dias_ventana` días
    donde compraron `min_insiders` o más personas distintas.

    No busco "alguna vez en la historia hubo 3 insiders" — busco una
    ventana concreta y compacta, porque es la concentración temporal
    lo que hace la señal fuerte. Tres compras repartidas en 3 años no
    es lo mismo que tres compras en 60 días.

    La fecha_deteccion que devuelvo es el día en que el N-ésimo insider
    distinto compró — el día en que la señal quedó completa. Antes
    devolvía el final teórico de la ventana, lo que retrasaba la
    detección hasta 60 días respecto al momento real de la señal, y
    eso sesgaba el backtest hacia entradas tardías sin motivo.
    """
    if len(transacciones) < min_insiders:
        return {"cumple": False, "insiders_max": 0, "fecha_deteccion": None}

    mejor_conteo = 0
    fecha_mejor = None

    for fecha_i, _ in transacciones:
        fecha_limite = fecha_i + timedelta(days=dias_ventana)
        insiders_en_ventana = set()
        fecha_completado = None

        # transacciones viene ordenada por fecha, así que el momento en
        # que el set alcanza el umbral es la fecha real de la señal.
        for fecha_j, nombre_j in transacciones:
            if fecha_i <= fecha_j <= fecha_limite and nombre_j not in insiders_en_ventana:
                insiders_en_ventana.add(nombre_j)
                if len(insiders_en_ventana) == min_insiders:
                    fecha_completado = fecha_j

        if len(insiders_en_ventana) > mejor_conteo:
            mejor_conteo = len(insiders_en_ventana)
            fecha_mejor = fecha_completado

        if mejor_conteo >= min_insiders:
            return {
                "cumple": True,
                "insiders_max": mejor_conteo,
                "fecha_deteccion": fecha_mejor,
            }

    return {"cumple": False, "insiders_max": mejor_conteo, "fecha_deteccion": None}


def hay_csuite_en_ventana(transacciones_con_cargo: list, fecha_deteccion,
                          dias_ventana: int) -> bool:
    """
    De las transacciones que forman el cluster, compruebo si al menos
    una tiene un cargo de C-suite. No basta con que 3 directores
    independientes compren — necesito que al menos uno tenga acceso
    real a los números del día a día.

    Importante: solo miro las compras dentro de la ventana del cluster
    ([fecha_deteccion - dias_ventana, fecha_deteccion]), no toda la
    historia. Sin este filtro, un CEO que compró hace ocho años
    validaba un cluster de directores de hoy — que es exactamente lo
    contrario de lo que quiere decir "el cluster incluye C-suite".
    """
    if fecha_deteccion is None:
        return False
    inicio_ventana = fecha_deteccion - timedelta(days=dias_ventana)
    for fecha, cargo in transacciones_con_cargo:
        if fecha is None or not (inicio_ventana <= fecha <= fecha_deteccion):
            continue
        if es_cargo_csuite(cargo):
            return True
    return False


def obtener_transacciones_compra(conn, empresa_id: int) -> list:
    """Traigo todas las compras P de la empresa, con nombre y cargo del insider."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select fecha_transaccion, nombre_insider, cargo
            from insider_transactions
            where empresa_id = %s and tipo_transaccion = 'P'
            order by fecha_transaccion
            """,
            (empresa_id,)
        )
        return cur.fetchall()
    finally:
        cur.close()


def evaluar_empresa(conn, empresa_id: int, market_cap, bolsa: str, config: dict) -> tuple:
    """
    Aplico los cuatro filtros en orden. Devuelvo (paso_todo: bool, motivo: str).
    Corto en cuanto falla uno — no tiene sentido seguir evaluando el resto
    si ya sé que la empresa no pasa.
    """
    cap_min = int(config.get("market_cap_min", 50_000_000))
    cap_max = int(config.get("market_cap_max", 2_000_000_000))
    dias_ventana = int(config.get("dias_ventana_cluster", 60))
    min_insiders = int(config.get("min_insiders_cluster", 3))

    if market_cap is None:
        return False, "Sin market cap disponible"

    if not (cap_min <= market_cap <= cap_max):
        return False, f"Market cap {market_cap:,} fuera de rango [{cap_min:,}-{cap_max:,}]"

    if not bolsa_es_valida(bolsa):
        return False, f"Bolsa no válida: {bolsa}"

    transacciones = obtener_transacciones_compra(conn, empresa_id)
    fechas_nombres = [(f, n) for f, n, c in transacciones]

    cluster = detectar_cluster_buying(fechas_nombres, dias_ventana, min_insiders)
    if not cluster["cumple"]:
        return False, (
            f"Sin cluster buying suficiente — máximo {cluster['insiders_max']} "
            f"insiders distintos en {dias_ventana} días (necesito {min_insiders})"
        )

    fechas_cargos = [(f, c) for f, n, c in transacciones]
    if not hay_csuite_en_ventana(fechas_cargos, cluster["fecha_deteccion"], dias_ventana):
        return False, "Cluster detectado pero ningún comprador del cluster es C-suite (CEO/CFO/COO/President)"

    return True, f"OK — {cluster['insiders_max']} insiders en ventana de {dias_ventana}d, con C-suite"


def actualizar_estado(conn, empresa_id: int, paso: bool, motivo: str):
    cur = conn.cursor()
    try:
        if paso:
            cur.execute(
                "update empresas set estado = 'filtros_ok', razon_descarte = null where id = %s",
                (empresa_id,)
            )
        else:
            cur.execute(
                "update empresas set estado = 'descartada', razon_descarte = %s where id = %s",
                (motivo, empresa_id)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"No pude actualizar estado de empresa {empresa_id}: {e}")
    finally:
        cur.close()


def obtener_empresas_con_mercado(conn, ticker: str = None) -> list:
    cur = conn.cursor()
    try:
        if ticker:
            cur.execute(
                "select id, ticker, market_cap_usd, bolsa from empresas where ticker = %s",
                (ticker,)
            )
        else:
            # Consulto la tabla directamente en vez de la vista empresas_activas.
            # Las vistas creadas con select * congelan su lista de columnas en
            # el momento de crearse: si añado una columna a la tabla después
            # (como pasó con 'bolsa'), la vista no la ve hasta recrearla.
            # Consultar la tabla con las condiciones explícitas evita ese
            # problema y hace el script independiente del estado de la vista.
            cur.execute(
                """
                select id, ticker, market_cap_usd, bolsa
                from empresas
                where activa = true
                  and market_cap_usd is not null
                """
            )
        return cur.fetchall()
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(description="Aplica los filtros binarios de la Capa 1")
    parser.add_argument("--ticker", type=str, help="Evalúa solo esta empresa (para depurar)")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion(conn)
        empresas = obtener_empresas_con_mercado(conn, args.ticker)

        log.info(f"Evaluando {len(empresas)} empresas contra los filtros de Capa 1")

        pasaron, descartadas = 0, 0

        for empresa_id, ticker, market_cap, bolsa in empresas:
            paso, motivo = evaluar_empresa(conn, empresa_id, market_cap, bolsa, config)
            actualizar_estado(conn, empresa_id, paso, motivo)

            if paso:
                pasaron += 1
                log.info(f"{ticker}: PASA — {motivo}")
            else:
                descartadas += 1

        log.info(
            f"\nResumen final:\n"
            f"  Pasaron el filtro:  {pasaron}\n"
            f"  Descartadas:        {descartadas}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
