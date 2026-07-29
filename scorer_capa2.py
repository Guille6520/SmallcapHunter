"""
scorer_capa2.py — scoring numérico sobre las candidatas de la Capa 1

Solo puntúo las empresas que ya pasaron los filtros binarios (estado
'filtros_ok'). No tiene sentido gastar cálculo en las que ya descarté.

Cuatro sub-scores, cada uno de 0 a 10, que sumo en un score_total de 0 a 40.
Los guardo desagregados en la tabla auditorias para poder defender por qué
el sistema puntuó cada empresa como lo hizo — no solo el número final.

  1. score_precio (0-10) — posición en el rango de 52 semanas.
     Cuanto más cerca de mínimos, más alto el score. Es el predictor
     más fuerte según la literatura: un insider comprando cuando la
     acción está hundida tiene más convicción que uno comprando en máximos.

  2. score_conviccion (0-10) — cuántos insiders distintos compraron DENTRO
     DE LA VENTANA DEL CLUSTER y con qué intensidad. Más gente y más
     dinero concentrados en el tiempo = más señal.

  3. score_temporal (0-10) — aceleración del crecimiento. Aquí está el
     corazón de la hipótesis: no me importa que crezca, me importa que
     la TASA de crecimiento esté subiendo trimestre a trimestre.

  4. score_catalizador (0-10) — mejora de márgenes y viraje de caja.
     Una empresa que pierde dinero pero cuyo margen bruto mejora y cuya
     caja operativa vira hacia positivo está en la fase pre-explosiva.

Cómo usarlo:
  python scorer_capa2.py
  python scorer_capa2.py --ticker NUVB
"""

import os
import logging
import argparse
from datetime import timedelta
from typing import Optional

import psycopg2

from filtro_capa1 import detectar_cluster_buying
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


def score_precio(posicion_52w: Optional[float]) -> int:
    """
    Posición en el rango de 52 semanas: 0.0 = en mínimos, 1.0 = en máximos.
    Invierto la escala porque me interesa lo contrario de lo intuitivo:
    una empresa cerca de mínimos con insiders comprando es más interesante
    que una en máximos. El insider que compra barato arriesga más convicción.

    Reparto por tramos en vez de una fórmula lineal continua porque quiero
    poder explicar el corte con claridad en la defensa del proyecto.
    """
    if posicion_52w is None:
        return 0
    if posicion_52w <= 0.15:
        return 10   # muy cerca de mínimos de 52 semanas
    if posicion_52w <= 0.30:
        return 8
    if posicion_52w <= 0.45:
        return 6
    if posicion_52w <= 0.60:
        return 4
    if posicion_52w <= 0.80:
        return 2
    return 0        # cerca de máximos, poco margen contrarian


def score_conviccion(conn, empresa_id: int, config: dict) -> int:
    """
    Mido dos cosas y las combino: cuántos insiders distintos compraron
    (amplitud) y el importe total invertido (intensidad) — pero solo
    dentro de la ventana del cluster, no en toda la historia de la
    empresa. Antes contaba todo el histórico, y eso premiaba igual a
    6 insiders comprando en 60 días que a 6 repartidos en 10 años —
    justo la distinción que el filtro de cluster existe para hacer.
    Reutilizo detectar_cluster_buying para localizar la misma ventana
    exacta que validó la Capa 1, no una versión distinta.

    No tengo todavía el dato de compensación anual del insider (viene del
    DEF 14A, que aún no ingiero), así que de momento uso el importe absoluto
    como proxy de intensidad. Documento esto como limitación: el ratio ideal
    sería importe/compensación, no importe absoluto — un CEO invirtiendo el
    50% de su sueldo dice más que uno invirtiendo una cifra grande que para
    él es calderilla.
    """
    dias_ventana = int(config.get("dias_ventana_cluster", 60))
    min_insiders = int(config.get("min_insiders_cluster", 3))

    cur = conn.cursor()
    try:
        cur.execute(
            """
            select fecha_transaccion, nombre_insider, importe_total
            from insider_transactions
            where empresa_id = %s and tipo_transaccion = 'P'
            order by fecha_transaccion
            """,
            (empresa_id,)
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    cluster = detectar_cluster_buying(
        [(f, n) for f, n, _ in filas], dias_ventana, min_insiders
    )

    if cluster["fecha_deteccion"] is not None:
        inicio = cluster["fecha_deteccion"] - timedelta(days=dias_ventana)
        fin = cluster["fecha_deteccion"]
        filas = [x for x in filas if x[0] and inicio <= x[0] <= fin]
    # Si no hay cluster (no debería pasar: la empresa vino de Capa 1),
    # dejo todas las filas para no puntuar 0 en silencio por un desajuste
    # de datos — el log de Capa 1 es el sitio donde investigar eso.

    insiders = len({n for _, n, _ in filas if n})
    total = sum(float(imp) for _, _, imp in filas if imp is not None)

    # Componente de amplitud: cuántas personas distintas
    if insiders >= 6:
        amplitud = 5
    elif insiders >= 4:
        amplitud = 4
    elif insiders >= 3:
        amplitud = 3
    else:
        amplitud = 1

    # Componente de intensidad: cuánto dinero en total
    if total >= 5_000_000:
        intensidad = 5
    elif total >= 1_000_000:
        intensidad = 4
    elif total >= 250_000:
        intensidad = 3
    elif total >= 50_000:
        intensidad = 2
    else:
        intensidad = 1

    return amplitud + intensidad


def _serie_trimestral(conn, empresa_id: int, campo: str) -> list:
    """
    Traigo la serie de un campo (revenue, operating_cash_flow...) con su
    año y trimestre fiscal, ordenada de más antiguo a más reciente y
    saltando los NULL. Devuelvo tuplas (anio, trimestre, valor) para
    poder comprobar después si dos filas son trimestres consecutivos
    de verdad — sin el año/trimestre no hay forma de saberlo.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            select anio_fiscal, trimestre, {campo}
            from metricas_trimestrales
            where empresa_id = %s and {campo} is not null
            order by anio_fiscal, trimestre
            """,
            (empresa_id,)
        )
        return cur.fetchall()
    finally:
        cur.close()


def _indice_trimestre(anio: int, trimestre: int) -> int:
    """Trimestres como enteros consecutivos: 2023Q4 y 2024Q1 distan 1."""
    return anio * 4 + (trimestre - 1)


def score_temporal(conn, empresa_id: int) -> int:
    """
    El corazón de la hipótesis: no puntúo crecimiento, puntúo ACELERACIÓN.

    Calculo la tasa de crecimiento trimestre a trimestre del revenue, y
    luego miro si esa tasa está subiendo. Una empresa que crece 5%, 5%, 5%
    es lineal (aburrida). Una que crece 5%, 9%, 16% está acelerando — ese
    es el patrón pre-explosivo que busco.

    Solo calculo la tasa entre trimestres fiscales ADYACENTES. Si a la
    serie le falta un trimestre por un hueco de datos, antes empalmaba
    Q4-2023 con Q3-2024 como si fueran consecutivos y la "tasa QoQ"
    resultante era ficción. Prefiero perder ese par a medir basura.

    Necesito al menos 4 trimestres para ver una tendencia en la tasa.
    """
    filas = _serie_trimestral(conn, empresa_id, "revenue")

    # Filtro revenues negativos o cero: no puedo calcular tasa sobre ellos
    filas = [(a, t, r) for a, t, r in filas if r and r > 0]

    if len(filas) < 4:
        return 0

    # Uso los últimos 8 trimestres como máximo — más atrás es menos relevante
    filas = filas[-8:]

    # Tasa de crecimiento solo entre trimestres fiscales consecutivos
    tasas = []
    for (a0, t0, r0), (a1, t1, r1) in zip(filas, filas[1:]):
        if _indice_trimestre(a1, t1) - _indice_trimestre(a0, t0) == 1:
            tasas.append((r1 - r0) / r0)

    if len(tasas) < 3:
        return 0

    # Comparo la media de las tasas recientes contra las antiguas.
    # Si las recientes son mayores, hay aceleración.
    mitad = len(tasas) // 2
    tasas_antiguas = tasas[:mitad]
    tasas_recientes = tasas[mitad:]

    media_antigua = sum(tasas_antiguas) / len(tasas_antiguas)
    media_reciente = sum(tasas_recientes) / len(tasas_recientes)

    # Puntúo según cuánto se acelera
    if media_reciente <= 0:
        return 0   # está decreciendo
    if media_antigua <= 0:
        # Pasó de decrecer a crecer — viraje fuerte
        return 8

    aceleracion = (media_reciente - media_antigua) / abs(media_antigua)

    if aceleracion >= 1.0:
        return 10   # la tasa se ha más que duplicado
    if aceleracion >= 0.5:
        return 8
    if aceleracion >= 0.2:
        return 6
    if aceleracion >= 0.0:
        return 4    # crece pero sin acelerar
    return 2        # crece pero desacelerando


def score_catalizador(conn, empresa_id: int) -> int:
    """
    Busco dos señales de que las pérdidas son de expansión, no de deterioro:

    1. Margen bruto mejorando — aunque haya pérdidas netas, si el margen
       bruto sube trimestre a trimestre, el negocio escala bien.
    2. Caja operativa virando hacia positivo — el burn se reduce.

    Cada una vale hasta 5 puntos.
    """
    puntos = 0

    # Señal 1: margen bruto mejorando.
    # Traigo revenue y gross_profit DE LA MISMA FILA en una sola query.
    # Antes traía las dos series por separado y las alineaba por posición
    # con [-n:] — pero cada serie salta sus propios NULLs, así que si a
    # gross_profit le faltaban trimestres intermedios, acababa dividiendo
    # el gross de un trimestre entre el revenue de otro distinto.
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select gross_profit::FLOAT / revenue
            from metricas_trimestrales
            where empresa_id = %s
              and revenue is not null and revenue > 0
              and gross_profit is not null
            order by anio_fiscal, trimestre
            """,
            (empresa_id,)
        )
        margenes = [row[0] for row in cur.fetchall()][-8:]
    finally:
        cur.close()

    if len(margenes) >= 4:
        mitad = len(margenes) // 2
        margen_antiguo = sum(margenes[:mitad]) / mitad
        margen_reciente = sum(margenes[mitad:]) / (len(margenes) - mitad)
        if margen_reciente > margen_antiguo:
            puntos += 5

    # Señal 2: caja operativa virando hacia positivo
    fcf = [v for _, _, v in _serie_trimestral(conn, empresa_id, "operating_cash_flow")]
    if len(fcf) >= 4:
        fcf = fcf[-4:]
        # ¿Los trimestres recientes son mejores que los antiguos?
        if fcf[-1] > fcf[0]:
            puntos += 5

    return puntos


def guardar_scores(conn, empresa_id: int, sp: int, sc: int, st: int, scat: int):
    """
    Guardo una fila en auditorias con los cuatro sub-scores y el total.
    También actualizo el estado de la empresa a 'scoring_ok' para saber que
    ya pasó por la Capa 2.
    """
    total = sp + sc + st + scat
    cur = conn.cursor()
    try:
        # Borro cualquier scoring anterior de esta empresa antes de insertar
        # el nuevo. auditorias guarda histórico de análisis, pero para el
        # scoring numérico me interesa solo el más reciente — si re-ejecuto
        # el scorer no quiero acumular filas duplicadas de la misma empresa.
        # Distingo las filas de scoring de las de análisis LLM porque estas
        # últimas tienen veredicto no nulo.
        cur.execute(
            "delete from auditorias where empresa_id = %s and veredicto is null",
            (empresa_id,)
        )
        cur.execute(
            """
            insert into auditorias (
                empresa_id, score_precio, score_conviccion,
                score_temporal, score_catalizador, score_total
            )
            values (%s, %s, %s, %s, %s, %s)
            """,
            (empresa_id, sp, sc, st, scat, total)
        )
        cur.execute(
            "update empresas set estado = 'scoring_ok' where id = %s",
            (empresa_id,)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar scores de empresa {empresa_id}: {e}")
    finally:
        cur.close()


def leer_configuracion(conn) -> dict:
    cur = conn.cursor()
    try:
        cur.execute("select clave, valor from configuracion")
        return {clave: valor for clave, valor in cur.fetchall()}
    finally:
        cur.close()


def obtener_candidatas(conn, ticker: str = None) -> list:
    cur = conn.cursor()
    try:
        if ticker:
            cur.execute(
                "select id, ticker, posicion_52w from empresas where ticker = %s",
                (ticker,)
            )
        else:
            cur.execute(
                """
                select id, ticker, posicion_52w
                from empresas
                where estado = 'filtros_ok'
                """
            )
        return cur.fetchall()
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(description="Scoring numérico de la Capa 2")
    parser.add_argument("--ticker", type=str, help="Puntúa solo esta empresa")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion(conn)
        candidatas = obtener_candidatas(conn, args.ticker)

        log.info(f"Puntuando {len(candidatas)} candidatas")

        resultados = []

        for empresa_id, ticker, posicion_52w in candidatas:
            sp = score_precio(float(posicion_52w) if posicion_52w is not None else None)
            sc = score_conviccion(conn, empresa_id, config)
            st = score_temporal(conn, empresa_id)
            scat = score_catalizador(conn, empresa_id)

            guardar_scores(conn, empresa_id, sp, sc, st, scat)

            total = sp + sc + st + scat
            resultados.append((ticker, total, sp, sc, st, scat))

        # Ordeno de mayor a menor score para ver las mejores arriba
        resultados.sort(key=lambda x: x[1], reverse=True)

        log.info("\nTop 15 candidatas por score total:")
        log.info(f"{'Ticker':<8} {'Total':>5} {'Precio':>7} {'Convic':>7} {'Tempor':>7} {'Catal':>6}")
        for ticker, total, sp, sc, st, scat in resultados[:15]:
            log.info(f"{ticker:<8} {total:>5} {sp:>7} {sc:>7} {st:>7} {scat:>6}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
