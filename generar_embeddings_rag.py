"""
generar_embeddings_rag.py — completa la Capa 4 (RAG): rellena las
columnas embedding de metricas_trimestrales y eventos_8k que ya existen
en el schema pero que, hasta ahora, se dejaban vacías a propósito (ver
el docstring de ingesta_10q.py).

No distingo aquí entre "corpus histórico" (el que trae
ingesta_10q_historico.py) y "candidatas activas" (el que trae
ingesta_10q.py): las dos pasan por metricas_trimestrales y las dos
sirven como referencia para trimestres futuros. La empresa que hoy es
una candidata activa es, dentro de un año, un trimestre histórico más
al que otras candidatas se pueden parecer.

Solo proceso filas con texto_mda/texto ya presente y embedding NULL —
así relanzar el script tras una interrupción no repite trabajo ni gasto
de API.

Cómo usarlo:
  python generar_embeddings_rag.py                  # metricas + eventos_8k
  python generar_embeddings_rag.py --tabla metricas
  python generar_embeddings_rag.py --tabla eventos
  python generar_embeddings_rag.py --limite 50       # prueba rápida
"""

import os
import time
import logging
import argparse

import psycopg2
from dotenv import load_dotenv

from embeddings import generar_embedding_documento, vector_a_literal_pgvector

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Igual que en detective.py: no embebo documentos gigantes por coste y
# porque el modelo de embeddings tiene su propio límite de tokens de
# entrada, más corto que el de un modelo de generación.
MAX_CARACTERES_EMBEDDING = 8000

# Pausa entre llamadas — el tier gratuito de Gemini también tiene límite
# por minuto para el endpoint de embeddings, no solo para generación.
PAUSA_ENTRE_LLAMADAS = float(os.getenv("PAUSA_EMBEDDINGS_SEGUNDOS", "1.5"))


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def procesar_metricas_trimestrales(conn, limite: int) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select id, texto_mda from metricas_trimestrales
            where texto_mda is not null and embedding is null
            order by id
            limit %s
            """,
            (limite,),
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    ok, fallos = 0, 0
    for fila_id, texto in filas:
        try:
            vector = generar_embedding_documento(texto[:MAX_CARACTERES_EMBEDDING])
            cur = conn.cursor()
            try:
                cur.execute(
                    "update metricas_trimestrales set embedding = %s::vector where id = %s",
                    (vector_a_literal_pgvector(vector), fila_id),
                )
                conn.commit()
                ok += 1
            finally:
                cur.close()
        except Exception as e:
            conn.rollback()
            log.error(f"metricas_trimestrales id={fila_id}: {e}")
            fallos += 1
        time.sleep(PAUSA_ENTRE_LLAMADAS)

    return {"tabla": "metricas_trimestrales", "procesadas": ok, "fallos": fallos, "total": len(filas)}


def procesar_eventos_8k(conn, limite: int) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select id, texto from eventos_8k
            where texto is not null and embedding is null
            order by id
            limit %s
            """,
            (limite,),
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    ok, fallos = 0, 0
    for fila_id, texto in filas:
        try:
            vector = generar_embedding_documento(texto[:MAX_CARACTERES_EMBEDDING])
            cur = conn.cursor()
            try:
                cur.execute(
                    "update eventos_8k set embedding = %s::vector where id = %s",
                    (vector_a_literal_pgvector(vector), fila_id),
                )
                conn.commit()
                ok += 1
            finally:
                cur.close()
        except Exception as e:
            conn.rollback()
            log.error(f"eventos_8k id={fila_id}: {e}")
            fallos += 1
        time.sleep(PAUSA_ENTRE_LLAMADAS)

    return {"tabla": "eventos_8k", "procesadas": ok, "fallos": fallos, "total": len(filas)}


def main():
    parser = argparse.ArgumentParser(
        description="Genera embeddings pendientes para el RAG de la Capa 4"
    )
    parser.add_argument("--tabla", choices=["metricas", "eventos", "todas"], default="todas")
    parser.add_argument("--limite", type=int, default=500,
                         help="Máximo de filas a procesar por tabla en esta pasada")
    args = parser.parse_args()

    conn = conectar_db()
    try:
        resultados = []
        if args.tabla in ("metricas", "todas"):
            resultados.append(procesar_metricas_trimestrales(conn, args.limite))
        if args.tabla in ("eventos", "todas"):
            resultados.append(procesar_eventos_8k(conn, args.limite))

        for r in resultados:
            log.info(
                f"{r['tabla']}: {r['procesadas']}/{r['total']} embeddings generados "
                f"({r['fallos']} fallos)"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
