"""
evaluacion_tesis_v2.py — mi evaluación PRIVADA de la tesis V2 (premiar máximos de
52 semanas) frente a la V1 (premiar mínimos) y al control V0 (sin posición).

Lo que hago aquí, en una frase: miro qué empresas marcó cada brazo en las
instantáneas que guardó scorer_v2.py, cuento cada empresa desde el día en que un
brazo la marcó por primera vez, y mido cuánto ha rendido por encima del IWM
(Russell 2000) a 12 y 24 meses, tal y como fijé en docs/tesis-v2.md.

Es de USO PERSONAL, igual que seguimiento_senales.py: trabajo con precios de
empresas concretas, y eso no es un resultado del proyecto ni consejo de inversión.
Por eso solo funciono si MOSTRAR_SEGUIMIENTO=1 está en el .env, y nada de lo que
imprimo se publica (ni README, ni dashboard, ni demo).

Qué NO hago, a propósito:
  - No toco las reglas ni las puntuaciones; solo leo lo que ya está guardado.
  - No cambio el criterio después de ver los números: los intervalos y el
    horizonte son los del pre-registro.
  - Antes de que se cumplan los 12 meses enseño una lectura "provisional" para
    ver cómo va, pero la marco como tal: no vale como evidencia a favor ni en contra.

Cómo usarlo:
  python evaluacion_tesis_v2.py --actualizar   # bajo los precios que falten y evalúo
  python evaluacion_tesis_v2.py                # evalúo con los precios que ya tengo
"""

import random
import logging
import argparse
from bisect import bisect_right
from datetime import date, timedelta
from statistics import median

from scorer_v2 import BRAZOS
from seguimiento_senales import BENCHMARK, activado, actualizar_precios, asegurar_esquema, leer_precios

log = logging.getLogger(__name__)

# Los dos horizontes del pre-registro, en días de calendario desde la primera marca.
HORIZONTES = {"12 meses": 365, "24 meses": 730}

BOOTSTRAP_REMUESTREOS = 5000
SEMILLA = 20261102  # fija, para que dos ejecuciones con los mismos datos den lo mismo


# ---------- cálculo (puro, sin base de datos) ----------

def primeras_marcas(filas: list) -> dict:
    """Recorro las instantáneas en orden de fecha y, para cada brazo, me quedo con
    el primer día en que marcó a cada empresa. Una empresa que vuelve a salir en
    instantáneas posteriores no cuenta otra vez: cuenta desde la primera.
    filas: dicts con fecha, ticker y candidata_v1 / candidata_v2 / candidata_v0.
    Devuelvo {brazo: {ticker: fecha}}."""
    marcas = {brazo: {} for brazo in BRAZOS}
    for f in sorted(filas, key=lambda f: f["fecha"]):
        for brazo in BRAZOS:
            if f[f"candidata_{brazo}"]:
                marcas[brazo].setdefault(f["ticker"], f["fecha"])
    return marcas


def _cierre_hasta(serie: list, fecha: date):
    """El último cierre de la serie [(fecha, cierre)] en esa fecha o antes, o None
    si la serie aún no había empezado."""
    fechas = [f for f, _ in serie]
    i = bisect_right(fechas, fecha) - 1
    return (serie[i][0], float(serie[i][1])) if i >= 0 else None


def medir(fecha_marca: date, precios: list, bench: list, dias: int = None):
    """Mido una empresa desde el día en que la marcó un brazo: retorno y exceso
    sobre el IWM. El precio de partida es el último cierre del día de la marca o
    anterior (el mismo criterio que el seguimiento).
    Con dias=None miro hasta el último precio que tengo (lectura provisional). Con
    dias=365, por ejemplo, miro a esa distancia y devuelvo None si todavía no he
    llegado a esa fecha: no relleno con lo que haya, porque eso falsearía el
    horizonte. También devuelvo None si me falta el precio de partida."""
    if not precios or not bench:
        return None
    inicio = _cierre_hasta(precios, fecha_marca)
    b_inicio = _cierre_hasta(bench, fecha_marca)
    if inicio is None or b_inicio is None or not inicio[1]:
        return None

    if dias is None:
        fin, b_fin = precios[-1], _cierre_hasta(bench, precios[-1][0])
    else:
        objetivo = fecha_marca + timedelta(days=dias)
        if precios[-1][0] < objetivo or bench[-1][0] < objetivo:
            return None  # todavía no ha pasado el horizonte
        fin, b_fin = _cierre_hasta(precios, objetivo), _cierre_hasta(bench, objetivo)
    if fin is None or b_fin is None:
        return None

    retorno = float(fin[1]) / inicio[1] - 1
    retorno_bench = b_fin[1] / b_inicio[1] - 1
    return {"retorno": retorno, "exceso": retorno - retorno_bench}


def medidas_de_un_brazo(marcas_brazo: dict, precios_por_ticker: dict, bench: list, dias: int = None):
    """Mido todas las empresas de un brazo. Devuelvo ({ticker: medida}, [sin_medir]):
    cuento aparte las que no pude medir (por horizonte aún no cumplido o por no
    tener precios) en vez de ignorarlas en silencio."""
    medidas, sin_medir = {}, []
    for ticker, fecha in marcas_brazo.items():
        m = medir(fecha, precios_por_ticker.get(ticker, []), bench, dias)
        if m is None:
            sin_medir.append(ticker)
        else:
            medidas[ticker] = m
    return medidas, sin_medir


def resumen(medidas: dict) -> dict:
    """Resumo un brazo con lo que dice el pre-registro: cuántas empresas, mediana
    del exceso sobre el IWM, mediana del retorno y qué porcentaje dobló (retorno
    de +100 % o más)."""
    if not medidas:
        return {"n": 0, "mediana_exceso": None, "mediana_retorno": None, "pct_dobla": None}
    excesos = [m["exceso"] for m in medidas.values()]
    retornos = [m["retorno"] for m in medidas.values()]
    return {
        "n": len(medidas),
        "mediana_exceso": median(excesos),
        "mediana_retorno": median(retornos),
        "pct_dobla": sum(1 for r in retornos if r >= 1.0) / len(retornos),
    }


def bootstrap_diferencia(medidas_a: dict, medidas_b: dict, remuestreos: int = BOOTSTRAP_REMUESTREOS,
                         semilla: int = SEMILLA):
    """Diferencia de medianas de exceso (A menos B) con un intervalo al 95 %.
    Remuestreo EMPRESAS, no instantáneas: pongo en un saco todas las empresas que
    mide cualquiera de los dos brazos, saco empresas con reemplazo y, en cada
    remuestreo, calculo la mediana de cada brazo con las que le tocan. Así las
    empresas compartidas entre brazos se mueven juntas, que es lo honesto.
    Devuelvo (diferencia, bajo, alto, remuestreos_validos), o None si algún brazo
    está vacío. El intervalo es optimista: las empresas comparten mercado y época,
    y eso no lo corrige el remuestreo (lo dije en los límites del pre-registro)."""
    if not medidas_a or not medidas_b:
        return None
    universo = sorted(set(medidas_a) | set(medidas_b))
    punto = median(m["exceso"] for m in medidas_a.values()) - median(m["exceso"] for m in medidas_b.values())

    azar = random.Random(semilla)
    diferencias = []
    for _ in range(remuestreos):
        muestra = [azar.choice(universo) for _ in universo]
        a = [medidas_a[t]["exceso"] for t in muestra if t in medidas_a]
        b = [medidas_b[t]["exceso"] for t in muestra if t in medidas_b]
        if a and b:
            diferencias.append(median(a) - median(b))
    if not diferencias:
        return None
    diferencias.sort()
    bajo = diferencias[int(0.025 * (len(diferencias) - 1))]
    alto = diferencias[int(round(0.975 * (len(diferencias) - 1)))]
    return punto, bajo, alto, len(diferencias)


def lectura(diferencia) -> str:
    """Traduzco el intervalo a una de las tres lecturas del pre-registro, sin
    adornos: o excluye el 0, o lo incluye, o no hay datos."""
    if diferencia is None:
        return "sin datos"
    _, bajo, alto, _ = diferencia
    if bajo > 0:
        return "a favor del primero (el intervalo excluye 0)"
    if alto < 0:
        return "a favor del segundo (el intervalo excluye 0)"
    return "no concluyente (el intervalo incluye 0)"


# ---------- base de datos ----------

def leer_marcas(conn) -> dict:
    """Leo las instantáneas que guardó scorer_v2.py y saco la primera marca de
    cada empresa en cada brazo. Si la tabla aún no existe (no se ha guardado
    ninguna instantánea), devuelvo todo vacío."""
    cur = conn.cursor()
    try:
        cur.execute("select to_regclass('tesis_instantaneas')")
        if cur.fetchone()[0] is None:
            return {brazo: {} for brazo in BRAZOS}
        cur.execute(
            "select fecha_instantanea, ticker, candidata_v1, candidata_v2, candidata_v0 "
            "from tesis_instantaneas order by fecha_instantanea"
        )
        filas = [{"fecha": f, "ticker": t, "candidata_v1": a, "candidata_v2": b, "candidata_v0": c}
                 for f, t, a, b, c in cur.fetchall()]
    finally:
        cur.close()
    return primeras_marcas(filas)


def evaluar(conn, actualizar: bool = False) -> dict:
    """Junto todo: marcas por brazo, precios (los bajo de Yahoo si me lo piden) y
    las medidas a cada horizonte y la provisional."""
    asegurar_esquema(conn)
    marcas = leer_marcas(conn)

    # Los precios de las empresas que solo marca un brazo (y que quizá nunca
    # analizaron los agentes) no los baja el seguimiento normal, así que los pido
    # yo aquí, con la fecha de su primera marca en cualquier brazo.
    primera_en_alguno = {}
    for brazo_marcas in marcas.values():
        for t, f in brazo_marcas.items():
            primera_en_alguno[t] = min(f, primera_en_alguno.get(t, f))
    if actualizar and primera_en_alguno:
        actualizar_precios(conn, [{"ticker": t, "fecha": f} for t, f in primera_en_alguno.items()])

    precios = leer_precios(conn, set(primera_en_alguno) | {BENCHMARK})
    bench = precios.get(BENCHMARK, [])

    salida = {"marcas": marcas, "brazos": {}}
    for brazo, marcas_brazo in marcas.items():
        salida["brazos"][brazo] = {}
        lecturas = {"provisional": None, **HORIZONTES}
        for nombre, dias in lecturas.items():
            medidas, sin_medir = medidas_de_un_brazo(marcas_brazo, precios, bench, dias)
            salida["brazos"][brazo][nombre] = {"medidas": medidas, "sin_medir": sin_medir,
                                               "resumen": resumen(medidas)}
    return salida


def _pct(x):
    return "   s/d" if x is None else f"{100 * x:+6.1f}%"


def imprimir(salida: dict) -> None:
    """Imprimo lo que he medido, solo para mí. Primero la lectura provisional (cómo
    van hasta hoy) y luego los dos horizontes del pre-registro, que son los que
    cuentan y que de momento estarán vacíos hasta que pase el tiempo."""
    n_marcadas = {b: len(m) for b, m in salida["marcas"].items()}
    print(f"\nEmpresas marcadas por brazo: " + " | ".join(f"{b.upper()} {n}" for b, n in n_marcadas.items()))

    for nombre in ("provisional", *HORIZONTES):
        titulo = ("PROVISIONAL, hasta hoy (no es evidencia: aún no ha pasado el horizonte)"
                  if nombre == "provisional" else f"A {nombre.upper()} (el que cuenta)")
        print(f"\n{titulo}")
        print(f"  {'brazo':6} {'n':>3} {'sin medir':>9} {'mediana exceso':>15} {'mediana retorno':>16} {'% dobla':>8}")
        for brazo in BRAZOS:
            d = salida["brazos"][brazo][nombre]
            r = d["resumen"]
            dobla = "   s/d" if r["pct_dobla"] is None else f"{100 * r['pct_dobla']:5.0f}%"
            print(f"  {brazo.upper():6} {r['n']:3d} {len(d['sin_medir']):9d} "
                  f"{_pct(r['mediana_exceso']):>15} {_pct(r['mediana_retorno']):>16} {dobla:>8}")
        for a, b in (("v2", "v1"), ("v2", "v0"), ("v1", "v0")):
            dif = bootstrap_diferencia(salida["brazos"][a][nombre]["medidas"], salida["brazos"][b][nombre]["medidas"])
            if dif is None:
                print(f"  {a.upper()} menos {b.upper()}: sin datos")
            else:
                punto, bajo, alto, _ = dif
                print(f"  {a.upper()} menos {b.upper()}: {100 * punto:+.1f} puntos, IC 95 % [{100 * bajo:+.1f}, {100 * alto:+.1f}] "
                      f"-> {lectura(dif).replace('primero', a.upper()).replace('segundo', b.upper())}")

    sin_precios = sorted({t for b in BRAZOS for t in salida["brazos"][b]["provisional"]["sin_medir"]})
    if sin_precios:
        print(f"\nNo tengo precios guardados de: {', '.join(sin_precios)}. Si no he lanzado --actualizar, es que "
              f"faltan por bajar; si ya lo hice, Yahoo no las tiene y no las puedo seguir "
              f"(ojo: eso sesga hacia lo optimista).")


def main():
    parser = argparse.ArgumentParser(description="Evaluación privada de la tesis V2 (uso personal)")
    parser.add_argument("--actualizar", action="store_true", help="bajo primero los precios que falten de Yahoo")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

    # Si no está activado el seguimiento personal, no hago nada: ni leo precios ni
    # llamo a Yahoo. Es la misma puerta que usa seguimiento_senales.py.
    if not activado():
        print("Evaluación de la tesis V2 desactivada: es de uso personal. "
              "Pon MOSTRAR_SEGUIMIENTO=1 en tu .env local para usarla.")
        return

    from orquestador import conectar_db
    conn = conectar_db()
    try:
        salida = evaluar(conn, actualizar=args.actualizar)
    finally:
        conn.close()
    imprimir(salida)


if __name__ == "__main__":
    main()
