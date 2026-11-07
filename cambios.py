"""
cambios.py — qué cambió desde la última pasada.

Entre pasada y pasada pasan cosas que hoy hay que deducir mirando listas: empresas
que entran o salen de las candidatas, scores que suben o bajan, y análisis que
cambian de veredicto. Esto lo junta en un solo sitio.

El problema de fondo: scorer_capa2.py borra y reescribe el score de cada empresa en
cada pasada, así que NO queda historial de quién estaba en la lista. Por eso cada
pasada guarda una foto (tabla candidatas_historial: fecha, empresa, score) y las
entradas, salidas y cambios de score se calculan comparando las dos últimas. Hasta
que haya dos fotos no hay nada que comparar, y se dice, en vez de enseñar una lista
vacía como si no hubiera cambiado nada.

Los cambios de veredicto sí tienen historial desde siempre: cada análisis del
Detective es una fila. Se compara el veredicto REAL del modelo (veredicto_preliminar)
con el que ese mismo modelo dio la vez anterior.

Es información para quien mira. No entra en el score ni lo leen los agentes, y no
recomienda nada: dice qué se movió.

Cómo usarlo:
  python cambios.py            # guarda la foto de hoy y enseña qué cambió
El orquestador guarda la foto en cada pasada, justo después de puntuar.
"""

import logging
from datetime import date

log = logging.getLogger(__name__)

# Un score que se mueve menos que esto es ruido de redondeo y de datos de mercado.
UMBRAL_CAMBIO_SCORE = 3
# Ventana por defecto para los cambios de veredicto.
DIAS_VEREDICTOS = 30

DDL = """
create table if not exists candidatas_historial (
    fecha       date    not null,
    empresa_id  integer not null,
    score_total integer not null,
    primary key (fecha, empresa_id)
);
"""


# ---------- cálculo (puro, sin base de datos) ----------

def comparar(anterior: dict, actual: dict, umbral: int = UMBRAL_CAMBIO_SCORE) -> dict:
    """anterior y actual: {empresa_id: score_total}. Devuelve quién entra, quién sale
    y a quién le cambia el score al menos `umbral` puntos."""
    entran = sorted(actual.keys() - anterior.keys(), key=lambda e: -actual[e])
    salen = sorted(anterior.keys() - actual.keys(), key=lambda e: -anterior[e])
    suben, bajan = [], []
    for e in actual.keys() & anterior.keys():
        dif = actual[e] - anterior[e]
        if dif >= umbral:
            suben.append((e, anterior[e], actual[e]))
        elif dif <= -umbral:
            bajan.append((e, anterior[e], actual[e]))
    suben.sort(key=lambda t: t[1] - t[2])      # las que más suben, primero
    bajan.sort(key=lambda t: t[2] - t[1])
    return {"entran": entran, "salen": salen, "suben": suben, "bajan": bajan}


# ---------- la base de datos ----------

def asegurar_esquema(conn):
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        conn.commit()
    finally:
        cur.close()


def guardar_foto(conn, fecha: date = None) -> int:
    """Guarda el score actual de cada empresa puntuada con la fecha de hoy. Si se
    ejecuta dos veces el mismo día, la segunda sustituye a la primera (una foto por
    día). Devuelve cuántas empresas guardó."""
    asegurar_esquema(conn)
    fecha = fecha or date.today()
    cur = conn.cursor()
    try:
        # Se borra la foto del día y se vuelve a hacer, en una sola transacción: si una
        # empresa salió de las candidatas entre dos ejecuciones del mismo día, su fila
        # anterior no puede quedarse y esconder que salió.
        cur.execute("delete from candidatas_historial where fecha = %s", (fecha,))
        cur.execute(
            """
            insert into candidatas_historial (fecha, empresa_id, score_total)
            select %s, a.empresa_id, a.score_total
            from auditorias a join empresas e on e.id = a.empresa_id
            where a.veredicto is null and e.activa and a.score_total is not null
            """,
            (fecha,),
        )
        guardadas = cur.rowcount
        conn.commit()
        return guardadas
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _dos_ultimas_fotos(cur):
    cur.execute("select distinct fecha from candidatas_historial order by fecha desc limit 2")
    fechas = [f for (f,) in cur.fetchall()]
    return (fechas[0], fechas[1]) if len(fechas) == 2 else (None, None)


def leer_cambios_de_lista(conn) -> dict:
    """Entradas, salidas y cambios de score entre las dos últimas fotos, con ticker y
    nombre. Si aún no hay dos fotos: {'hay_historial': False}."""
    asegurar_esquema(conn)
    cur = conn.cursor()
    try:
        nueva, vieja = _dos_ultimas_fotos(cur)
        if nueva is None:
            return {"hay_historial": False}
        cur.execute("select fecha, empresa_id, score_total from candidatas_historial where fecha in (%s, %s)",
                    (nueva, vieja))
        fotos = {nueva: {}, vieja: {}}
        for f, e, s in cur.fetchall():
            fotos[f][e] = s
        cambios = comparar(fotos[vieja], fotos[nueva])

        ids = set(cambios["entran"]) | set(cambios["salen"]) | {e for e, _, _ in cambios["suben"] + cambios["bajan"]}
        nombres = {}
        if ids:
            cur.execute("select id, ticker, nombre from empresas where id = any(%s)", (list(ids),))
            nombres = {i: (t, n) for i, t, n in cur.fetchall()}
        etiqueta = lambda e: nombres.get(e, ("?", ""))  # noqa: E731
        return {
            "hay_historial": True, "desde": vieja, "hasta": nueva,
            "entran": [(*etiqueta(e), fotos[nueva][e]) for e in cambios["entran"]],
            "salen": [(*etiqueta(e), fotos[vieja][e]) for e in cambios["salen"]],
            "suben": [(*etiqueta(e), antes, ahora) for e, antes, ahora in cambios["suben"]],
            "bajan": [(*etiqueta(e), antes, ahora) for e, antes, ahora in cambios["bajan"]],
        }
    finally:
        cur.close()


def leer_cambios_de_veredicto(conn, dias: int = DIAS_VEREDICTOS) -> list:
    """Análisis de los últimos `dias` cuyo veredicto real difiere del que ese mismo
    modelo dio la vez anterior sobre la misma empresa. (ticker, nombre, modelo,
    antes, ahora, fecha), los más recientes primero."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select ticker, nombre, modelo_llm, previo, actual, fecha from (
                select e.ticker, e.nombre, a.modelo_llm,
                       lag(a.respuesta_llm->>'veredicto_preliminar')
                           over (partition by a.empresa_id, a.modelo_llm order by a.fecha_analisis) as previo,
                       a.respuesta_llm->>'veredicto_preliminar' as actual,
                       a.fecha_analisis::date as fecha
                from auditorias a join empresas e on e.id = a.empresa_id
                where a.respuesta_llm ? 'veredicto_preliminar' and a.veredicto is not null
            ) t
            where previo is not null and previo <> actual
              and fecha >= current_date - make_interval(days => %s)
            order by fecha desc, ticker
            """,
            (dias,),
        )
        return cur.fetchall()
    finally:
        cur.close()


def fase_cambios(conn=None) -> dict:
    """La pasada del orquestador: guarda la foto de hoy. Nunca la rompe."""
    try:
        from orquestador import conectar_db
        propia = conn is None
        conn = conn or conectar_db()
        try:
            n = guardar_foto(conn)
            log.info(f"Cambios entre pasadas: foto de {n} candidatas guardada")
            return {"guardadas": n}
        finally:
            if propia:
                conn.close()
    except Exception as e:
        log.warning(f"Cambios entre pasadas: no pude guardar la foto ({e})")
        return {"guardadas": 0}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
    from orquestador import conectar_db
    conn = conectar_db()
    try:
        print(f"Foto de hoy: {guardar_foto(conn)} empresas")
        lista = leer_cambios_de_lista(conn)
        if not lista["hay_historial"]:
            print("Todavía solo hay una foto: las entradas y salidas se verán a partir de la próxima pasada.")
        else:
            print(f"\nEntre {lista['desde']} y {lista['hasta']}:")
            for clave, texto in (("entran", "Entran"), ("salen", "Salen"), ("suben", "Suben de score"), ("bajan", "Bajan de score")):
                print(f"  {texto}: {len(lista[clave])}")
                for fila in lista[clave][:10]:
                    print("    ", *fila)
        veredictos = leer_cambios_de_veredicto(conn)
        print(f"\nCambios de veredicto en {DIAS_VEREDICTOS} días: {len(veredictos)}")
        for fila in veredictos[:15]:
            print("    ", *fila)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
