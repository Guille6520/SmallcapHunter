"""
descubrir_clusters_historicos.py — busco, sobre TODO el histórico de
TODAS las empresas, cuáles tuvieron alguna vez un cluster real de
insiders (3+ personas distintas comprando en una ventana compacta).

Esto es un script de solo lectura — no toca ninguna tabla, no modifica
nada del pipeline en producción. Existe para validar la hipótesis del
proyecto contra casos reales: ¿hay empresas en mis propios datos que
mostraron la señal exacta que definí, y que después se convirtieron
en large caps? Es la pregunta que me hizo Guille y que no puedo
responder adivinando tickers de memoria uno a uno.

Reutilizo detectar_cluster_buying() de filtro_capa1.py en vez de
reescribir la lógica — así comparo exactamente la misma señal que
uso en producción, no una versión distinta a medias.

Cómo usarlo:
  python descubrir_clusters_historicos.py
  python descubrir_clusters_historicos.py --min-market-cap 2000000000
"""

import os
import logging
import argparse
from collections import defaultdict

import psycopg2

from filtro_capa1 import detectar_cluster_buying, leer_configuracion
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


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def obtener_todas_las_compras(conn) -> dict:
    """
    Traigo TODAS las compras P de TODAS las empresas en una sola query,
    en vez de una consulta por empresa. Con más de 10.000 empresas,
    una query por empresa habría tardado muchísimo más — así lo hago
    en una sola pasada y agrupo en Python.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select empresa_id, fecha_transaccion, nombre_insider
            from insider_transactions
            where tipo_transaccion = 'P'
            order by empresa_id, fecha_transaccion
            """
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    por_empresa = defaultdict(list)
    for empresa_id, fecha, nombre in filas:
        por_empresa[empresa_id].append((fecha, nombre))
    return por_empresa


def obtener_info_empresas(conn) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            "select id, ticker, nombre, market_cap_usd, bolsa, activa, estado from empresas"
        )
        filas = cur.fetchall()
    finally:
        cur.close()
    return {fila[0]: fila[1:] for fila in filas}


def main():
    parser = argparse.ArgumentParser(
        description="Busca clusters históricos reales de insiders en toda la base de datos"
    )
    parser.add_argument(
        "--min-market-cap", type=float, default=2_000_000_000,
        help="Market cap actual a partir del cual considero 'large cap' (default: 2B)"
    )
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion(conn)
        dias_ventana = int(config.get("dias_ventana_cluster", 60))
        min_insiders = int(config.get("min_insiders_cluster", 3))

        log.info(f"Parámetros: {min_insiders}+ insiders distintos en {dias_ventana} días")
        log.info("Cargando todas las compras de insiders de la base de datos...")

        compras_por_empresa = obtener_todas_las_compras(conn)
        info_empresas = obtener_info_empresas(conn)

        log.info(f"Empresas con al menos una compra P: {len(compras_por_empresa)}")

        con_cluster = []

        for empresa_id, transacciones in compras_por_empresa.items():
            # Atajo barato: si ni siquiera hay suficientes compras en total,
            # no merece la pena correr el algoritmo de ventana deslizante.
            if len(transacciones) < min_insiders:
                continue

            resultado = detectar_cluster_buying(transacciones, dias_ventana, min_insiders)
            if not resultado["cumple"]:
                continue

            info = info_empresas.get(empresa_id)
            if not info:
                continue

            ticker, nombre, market_cap, bolsa, activa, estado = info
            con_cluster.append({
                "ticker": ticker,
                "nombre": nombre,
                "market_cap_actual": market_cap,
                "bolsa": bolsa,
                "activa": activa,
                "insiders_en_cluster": resultado["insiders_max"],
                "fecha_deteccion": resultado["fecha_deteccion"],
            })

        log.info(f"\nEmpresas con cluster histórico real en algún momento: {len(con_cluster)}")

        # Separo en dos grupos. El primero es el interesante para tu pregunta:
        # empresas que alguna vez tuvieron la señal exacta que busco, y que
        # HOY son large caps — son la evidencia empírica de si la hipótesis
        # se sostiene con datos reales, no con nombres elegidos de memoria.
        large_caps = [
            c for c in con_cluster
            if c["market_cap_actual"] and c["market_cap_actual"] > args.min_market_cap
        ]
        resto = [c for c in con_cluster if c not in large_caps]

        large_caps.sort(key=lambda c: c["market_cap_actual"], reverse=True)

        log.info(
            f"\n=== Empresas con cluster histórico que HOY son large cap "
            f"(market cap > {args.min_market_cap:,.0f}) ==="
        )
        log.info(f"{'Ticker':<8} {'Insiders':>9} {'Fecha detección':>16} {'Market cap hoy':>18}  Nombre")
        for c in large_caps[:40]:
            fecha_str = str(c["fecha_deteccion"]) if c["fecha_deteccion"] else "N/D"
            log.info(
                f"{c['ticker']:<8} {c['insiders_en_cluster']:>9} {fecha_str:>16} "
                f"{c['market_cap_actual']:>18,.0f}  {c['nombre']}"
            )

        log.info(f"\nTotal con cluster histórico y hoy large cap: {len(large_caps)}")
        log.info(f"Total con cluster histórico y hoy small/mid cap o sin dato: {len(resto)}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
