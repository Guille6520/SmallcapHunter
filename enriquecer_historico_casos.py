"""
enriquecer_historico_casos.py — relleno los trimestres XBRL de la ÉPOCA
para los casos de un experimento histórico.

Por qué existe: el enriquecedor normal descarga los últimos ~8
trimestres de cada empresa activa. Para las empresas graduadas (hoy
>2B) eso significa 2024-2026 — y sus clusters de 2015-2023 se quedan
sin trimestres de la época con los que emparejar el 10-Q ni construir
la serie financiera del test ciego. Este script coge los casos de un
CSV (validadas o ganadoras) y re-enriquece cada empresa con una
ventana LARGA (48 trimestres = 12 años), reutilizando la misma función
de producción — no reinvento la extracción.

Es idempotente: guardar_trimestres hace on conflict, así que
re-ejecutarlo no duplica nada.

Cómo usarlo:
  python enriquecer_historico_casos.py --csv resultados_test_ganadoras.csv
  python enriquecer_historico_casos.py --csv validadas_historicas.csv
"""

import os
import csv
import time
import logging
import argparse

import psycopg2
from dotenv import load_dotenv

load_dotenv()

from enriquecedor_xbrl import enriquecer_empresa

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

VENTANA_HISTORICA = 48   # 12 años de trimestres — cubre cualquier cluster de la ventana
PAUSA = 0.3


def tickers_del_csv(ruta: str) -> list:
    """Los tickers del CSV, en cualquiera de los dos formatos, sin duplicados."""
    vistos, resultado = set(), []
    with open(ruta, encoding="utf-8") as f:
        lector = csv.DictReader(f)
        es_ganadoras = "fecha_cluster" in (lector.fieldnames or [])
        for fila in lector:
            if es_ganadoras and str(fila.get("tuvo_cluster", "")).lower() not in ("true", "1"):
                continue
            t = fila["ticker"]
            if t not in vistos:
                vistos.add(t)
                resultado.append(t)
    return resultado


def main():
    parser = argparse.ArgumentParser(
        description="Re-enriquece con XBRL de la época las empresas de un CSV de casos"
    )
    parser.add_argument("--csv", required=True)
    args = parser.parse_args()

    tickers = tickers_del_csv(args.csv)
    log.info(f"Empresas a re-enriquecer (ventana {VENTANA_HISTORICA} trimestres): {len(tickers)}")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    ok, sin_datos, no_encontradas = 0, 0, 0
    try:
        for i, ticker in enumerate(tickers, 1):
            cur.execute("select id, cik from empresas where ticker = %s", (ticker,))
            fila = cur.fetchone()
            if not fila:
                no_encontradas += 1
                continue
            empresa_id, cik = fila

            resultado = enriquecer_empresa(conn, empresa_id, cik, VENTANA_HISTORICA)
            if resultado.startswith("ok"):
                ok += 1
                log.info(f"{ticker}: {resultado}")
            else:
                sin_datos += 1
                log.info(f"{ticker}: {resultado}")

            if i % 20 == 0:
                log.info(f"Progreso: {i}/{len(tickers)}")
            time.sleep(PAUSA)
    finally:
        cur.close()
        conn.close()

    log.info(
        f"\nResumen: correctas {ok} | sin datos XBRL {sin_datos} | "
        f"no encontradas en BD {no_encontradas}"
    )


if __name__ == "__main__":
    main()
