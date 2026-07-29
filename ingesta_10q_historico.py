"""
ingesta_10q_historico.py — construyo el corpus de referencia histórico
para el RAG de la Capa 3, usando una muestra de las empresas que
validacion_historica.py confirmó que de verdad tuvieron el patrón
cuando eran pequeñas.

Esto es distinto de ingesta_10q.py en un punto crítico: aquél trae el
10-Q MÁS RECIENTE de hoy, que tiene sentido para las candidatas
actuales. Aquí necesito el 10-Q DE LA ÉPOCA del cluster — si Bloom
Energy tuvo su cluster en 2018, quiero el 10-Q que existía en 2018,
no el de 2026. Usar el de hoy sería mezclar pasado y presente otra vez,
el mismo error que ya corregimos en el market cap histórico.

Requiere que ya hayas ejecutado validacion_historica.py, porque leo
su archivo validadas_historicas.csv como fuente de la lista.

Cómo usarlo:
  python ingesta_10q_historico.py --muestra 40
"""

import os
import csv
import time
import logging
import argparse
from datetime import datetime

import requests
import psycopg2

from ingesta_10q import (
    SEC_HEADERS, descargar_y_extraer_mda, encontrar_trimestre_correspondiente,
    guardar_texto_mda,
)
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

PAUSA_ENTRE_REQUESTS = 0.15


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def leer_csv_validadas(ruta: str) -> list:
    """
    Acepto el CSV de validadas y también el de resultados del test de
    ganadoras (ahí filtro solo las que tuvieron cluster, y su fecha de
    detección es la del cluster). El empresa_id que falte se resuelve
    en main() por ticker.
    """
    filas = []
    with open(ruta, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        es_ganadoras = "fecha_cluster" in (reader.fieldnames or [])
        for row in reader:
            if es_ganadoras:
                if str(row.get("tuvo_cluster", "")).lower() not in ("true", "1"):
                    continue
                if not row.get("fecha_cluster"):
                    continue
                fecha = row["fecha_cluster"]
            else:
                fecha = row["fecha_deteccion"]
            filas.append({
                "empresa_id": int(row["empresa_id"]) if row.get("empresa_id") else None,
                "ticker": row["ticker"],
                "fecha_deteccion": datetime.strptime(fecha, "%Y-%m-%d").date(),
            })
    return filas


def muestrear_distribuido(filas: list, n: int) -> list:
    """
    En vez de coger las primeras N (que sesgaría hacia las fechas más
    antiguas, ya que el CSV viene ordenado por fecha), tomo una every
    k-ésima fila repartida a lo largo de toda la lista. Así el corpus
    de referencia cubre toda la ventana temporal (2015-2026), no solo
    un tramo.
    """
    if len(filas) <= n:
        return filas
    paso = len(filas) / n
    indices = [int(i * paso) for i in range(n)]
    return [filas[i] for i in indices]


def obtener_10q_en_fecha(cik: str, fecha_objetivo):
    """
    Busco el 10-Q más reciente que ya estuviera PRESENTADO en o antes
    de fecha_objetivo — nunca uno posterior, porque eso sería mirar
    información que en ese momento del cluster todavía no existía.

    A diferencia de obtener_ultimo_10q() (que coge el primer 10-Q de la
    lista, siempre el más reciente de hoy), aquí recorro todos los
    10-Q del histórico de la empresa y me quedo con el que tenga la
    fecha de filing más reciente que sea <= fecha_objetivo.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()

        def mejor_de_bloque(bloque, mejor):
            formularios = bloque.get("form", [])
            accessions = bloque.get("accessionNumber", [])
            fechas = bloque.get("filingDate", [])
            documentos = bloque.get("primaryDocument", [])
            for i, forma in enumerate(formularios):
                if forma != "10-Q":
                    continue
                fecha_filing = datetime.strptime(fechas[i], "%Y-%m-%d").date()
                if fecha_filing > fecha_objetivo:
                    continue
                if mejor is None or fecha_filing > mejor["fecha_filing"]:
                    mejor = {
                        "accession": accessions[i],
                        "fecha_filing": fecha_filing,
                        "documento": documentos[i],
                    }
            return mejor

        mejor = mejor_de_bloque(data.get("filings", {}).get("recent", {}), None)

        # El índice 'recent' solo lista los últimos ~1000 filings. Para
        # empresas longevas, el 10-Q de un cluster de 2015-2019 vive en
        # las páginas de archivo (filings.files) — sin recorrerlas, el
        # experimento histórico perdía justo los casos más antiguos.
        # Solo bajo las páginas cuyo rango de fechas pisa el objetivo.
        for pagina in data.get("filings", {}).get("files", []):
            desde = pagina.get("filingFrom", "1900-01-01")
            if datetime.strptime(desde, "%Y-%m-%d").date() > fecha_objetivo:
                continue
            try:
                r2 = requests.get(
                    f"https://data.sec.gov/submissions/{pagina['name']}",
                    headers=SEC_HEADERS, timeout=20,
                )
                r2.raise_for_status()
                mejor = mejor_de_bloque(r2.json(), mejor)
                time.sleep(PAUSA_ENTRE_REQUESTS)
            except Exception as e:
                log.warning(f"Página de archivo {pagina.get('name')}: {e}")

        if mejor:
            mejor["fecha_filing"] = mejor["fecha_filing"].isoformat()
        return mejor

    except Exception as e:
        log.warning(f"Error consultando filings históricos de CIK {cik}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Ingesta de 10-Q históricos para el corpus de referencia del RAG"
    )
    parser.add_argument("--csv", type=str, default="validadas_historicas.csv")
    parser.add_argument("--muestra", type=int, default=40)
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        log.error(
            f"No encuentro {args.csv}. Ejecuta primero validacion_historica.py, "
            f"que genera ese archivo."
        )
        return

    filas = leer_csv_validadas(args.csv)
    log.info(f"Total de validadas en el CSV: {len(filas)}")

    muestra = muestrear_distribuido(filas, args.muestra)
    log.info(f"Muestra a procesar: {len(muestra)} (repartida en el tiempo)")

    conn = conectar_db()
    cur = conn.cursor()

    con_seccion_exacta, con_fallback, sin_filing, errores = 0, 0, 0, 0

    try:
        for fila in muestra:
            empresa_id = fila["empresa_id"]
            ticker = fila["ticker"]
            fecha_det = fila["fecha_deteccion"]

            if empresa_id is None:
                cur.execute("select id from empresas where ticker = %s", (ticker,))
                row = cur.fetchone()
                if not row:
                    sin_filing += 1
                    continue
                empresa_id = row[0]
                fila["empresa_id"] = empresa_id

            cur.execute("select cik from empresas where id = %s", (empresa_id,))
            row = cur.fetchone()
            if not row:
                sin_filing += 1
                continue
            cik = row[0]

            filing = obtener_10q_en_fecha(cik, fecha_det)
            if not filing:
                log.warning(f"{ticker}: sin 10-Q disponible en la época de {fecha_det}")
                sin_filing += 1
                time.sleep(PAUSA_ENTRE_REQUESTS)
                continue

            texto, encontrado = descargar_y_extraer_mda(
                cik, filing["accession"], filing["documento"]
            )
            time.sleep(PAUSA_ENTRE_REQUESTS)

            if texto is None:
                errores += 1
                continue

            trimestre = encontrar_trimestre_correspondiente(
                conn, empresa_id, filing["fecha_filing"]
            )
            if not trimestre:
                log.warning(f"{ticker}: sin trimestre XBRL con el que emparejar")
                sin_filing += 1
                continue

            metrica_id, anio, trim = trimestre
            guardar_texto_mda(conn, metrica_id, texto)

            if encontrado:
                con_seccion_exacta += 1
                log.info(
                    f"{ticker}: MD&A histórico ({anio} Q{trim}, cluster {fecha_det}, "
                    f"{len(texto)} caracteres)"
                )
            else:
                con_fallback += 1
                log.info(f"{ticker}: fallback documento completo ({anio} Q{trim})")

        log.info(
            f"\nResumen final del corpus histórico:\n"
            f"  Sección MD&A aislada correctamente: {con_seccion_exacta}\n"
            f"  Fallback (documento completo):      {con_fallback}\n"
            f"  Sin 10-Q disponible en esa época:   {sin_filing}\n"
            f"  Errores:                            {errores}"
        )

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
