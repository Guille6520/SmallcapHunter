"""
scorer_v2.py — la tesis V2 en paralelo a la V1, sobre los mismos datos.

Lo ÚNICO que cambia entre V1, V2 y el control V0 es el sub-score de precio
(posición en el rango de 52 semanas). Todo lo demás (Capa 1, los otros tres
sub-scores, el corte) es idéntico, así que la comparación es limpia. Las reglas
están fijadas en docs/tesis-v2.md y no se editan durante el seguimiento.

No toca nada de producción: lee las puntuaciones que ya calculó scorer_capa2.py y
escribe SOLO en tesis_instantaneas (tabla nueva, la crea este script). Cada
ejecución guarda una instantánea de qué candidatas marca cada brazo ese día.

Cómo usarlo:
  python scorer_v2.py                  # guarda la instantánea y enseña la comparación
  python scorer_v2.py --sin-guardar    # solo enseña la comparación
"""

import os
import logging
import argparse
from datetime import date
from typing import Optional

import psycopg2
from dotenv import load_dotenv

from scorer_capa2 import score_precio as score_precio_v1

load_dotenv()
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

DDL = """
create table if not exists tesis_instantaneas (
    fecha_instantanea   date          not null,
    empresa_id          integer       not null references empresas(id),
    ticker              varchar(12)   not null,
    posicion_52w        decimal(6, 4),
    score_conviccion    integer,
    score_temporal      integer,
    score_catalizador   integer,
    total_v1            integer       not null,
    total_v2            integer       not null,
    total_v0            integer       not null,
    candidata_v1        boolean       not null,
    candidata_v2        boolean       not null,
    candidata_v0        boolean       not null,
    primary key (fecha_instantanea, empresa_id)
);
"""


def score_precio_v2(posicion_52w: Optional[float]) -> int:
    """El espejo EXACTO de V1: score_precio_v2(p) = score_precio_v1(1 - p). Premia
    estar cerca de MÁXIMOS de 52 semanas y no estar en caída libre (1.0 = en máximos,
    0.0 = en mínimos). Sin dato, 0 (igual que V1). Los tramos se escriben a mano, sin
    restar en coma flotante, para que los cortes sean exactos."""
    if posicion_52w is None:
        return 0
    if posicion_52w >= 0.85:
        return 10   # muy cerca de máximos de 52 semanas
    if posicion_52w >= 0.70:
        return 8
    if posicion_52w >= 0.55:
        return 6
    if posicion_52w >= 0.40:
        return 4
    if posicion_52w >= 0.20:
        return 2
    return 0        # cerca de mínimos: caída libre


def score_precio_v0(posicion_52w: Optional[float]) -> int:
    """El control: la posición no cuenta (5 siempre, con o sin dato)."""
    return 5


BRAZOS = {"v1": score_precio_v1, "v2": score_precio_v2, "v0": score_precio_v0}


def totales(fila: dict) -> dict:
    """Total 0-40 de cada brazo para una empresa: los tres sub-scores que no
    cambian más el score de precio de cada brazo."""
    resto = fila["score_conviccion"] + fila["score_temporal"] + fila["score_catalizador"]
    pos = fila["posicion_52w"]
    return {brazo: resto + f(pos) for brazo, f in BRAZOS.items()}


def comparar(filas: list, corte: int) -> dict:
    """filas: dicts con ticker, posicion_52w y los tres sub-scores. Devuelve qué
    empresas marca cada brazo y en qué se diferencian."""
    marcadas = {b: set() for b in BRAZOS}
    detalle = {}
    for f in filas:
        t = totales(f)
        detalle[f["ticker"]] = {**t, "posicion_52w": f["posicion_52w"]}
        for b, total in t.items():
            if total >= corte:
                marcadas[b].add(f["ticker"])
    return {
        "marcadas": marcadas,
        "detalle": detalle,
        "en_v1_y_v2": marcadas["v1"] & marcadas["v2"],
        "solo_v1": marcadas["v1"] - marcadas["v2"],
        "solo_v2": marcadas["v2"] - marcadas["v1"],
    }


def leer_puntuadas(conn) -> list:
    """Las empresas ya puntuadas por scorer_capa2.py, con sus sub-scores."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select e.id, e.ticker, e.posicion_52w,
                   a.score_conviccion, a.score_temporal, a.score_catalizador, a.score_total, a.score_precio
            from empresas e
            join auditorias a on a.empresa_id = e.id and a.veredicto is null
            where e.estado = 'scoring_ok'
            """
        )
        return [
            {"empresa_id": i, "ticker": t, "posicion_52w": float(p) if p is not None else None,
             "score_conviccion": sc, "score_temporal": st, "score_catalizador": scat,
             "total_guardado": tot, "score_precio_guardado": sp}
            for i, t, p, sc, st, scat, tot, sp in cur.fetchall()
        ]
    finally:
        cur.close()


def leer_corte(conn) -> int:
    cur = conn.cursor()
    try:
        cur.execute("select valor from configuracion where clave = 'score_minimo_llm'")
        fila = cur.fetchone()
        return int(fila[0]) if fila else 25
    finally:
        cur.close()


def guardar_instantanea(conn, filas: list, corte: int, fecha: date = None) -> int:
    fecha = fecha or date.today()
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        for f in filas:
            t = totales(f)
            cur.execute(
                """
                insert into tesis_instantaneas (
                    fecha_instantanea, empresa_id, ticker, posicion_52w,
                    score_conviccion, score_temporal, score_catalizador,
                    total_v1, total_v2, total_v0, candidata_v1, candidata_v2, candidata_v0
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (fecha_instantanea, empresa_id) do update set
                    posicion_52w = excluded.posicion_52w,
                    score_conviccion = excluded.score_conviccion,
                    score_temporal = excluded.score_temporal,
                    score_catalizador = excluded.score_catalizador,
                    total_v1 = excluded.total_v1, total_v2 = excluded.total_v2, total_v0 = excluded.total_v0,
                    candidata_v1 = excluded.candidata_v1, candidata_v2 = excluded.candidata_v2,
                    candidata_v0 = excluded.candidata_v0
                """,
                (fecha, f["empresa_id"], f["ticker"], f["posicion_52w"],
                 f["score_conviccion"], f["score_temporal"], f["score_catalizador"],
                 t["v1"], t["v2"], t["v0"], t["v1"] >= corte, t["v2"] >= corte, t["v0"] >= corte),
            )
        conn.commit()
        return len(filas)
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(description="Tesis V2 en paralelo a la V1")
    parser.add_argument("--sin-guardar", action="store_true", help="solo enseña la comparación")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        filas = leer_puntuadas(conn)
        corte = leer_corte(conn)
        # La V1 recalculada aquí tiene que dar lo mismo que lo que guardó el scorer de producción
        incoherentes = [f["ticker"] for f in filas if totales(f)["v1"] != f["total_guardado"]]
        if incoherentes:
            log.warning(f"V1 recalculada difiere del total guardado en {len(incoherentes)} empresas: {incoherentes[:8]}")

        c = comparar(filas, corte)
        if not args.sin_guardar:
            guardar_instantanea(conn, filas, corte)
    finally:
        conn.close()

    m = c["marcadas"]
    print(f"\n{len(filas)} empresas puntuadas | corte {corte}/40")
    print(f"candidatas  V1 (mínimos): {len(m['v1'])} | V2 (máximos): {len(m['v2'])} | V0 (sin posición): {len(m['v0'])}")
    print(f"en V1 y V2: {len(c['en_v1_y_v2'])} | solo V1: {len(c['solo_v1'])} | solo V2: {len(c['solo_v2'])}")
    for titulo, conj in (("SOLO V1 (mínimos)", c["solo_v1"]), ("SOLO V2 (máximos)", c["solo_v2"])):
        print(f"\n{titulo}")
        for t in sorted(conj, key=lambda x: -c["detalle"][x]["v1" if "V1" in titulo else "v2"]):
            d = c["detalle"][t]
            pos = "s/d" if d["posicion_52w"] is None else f"{d['posicion_52w']:.2f}"
            print(f"  {t:7} posición {pos:>5} | totales V1 {d['v1']:2d}  V2 {d['v2']:2d}  V0 {d['v0']:2d}")
    if not args.sin_guardar:
        print(f"\nInstantánea guardada ({date.today()}).")


if __name__ == "__main__":
    main()
