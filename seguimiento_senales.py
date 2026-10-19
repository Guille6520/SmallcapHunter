"""
seguimiento_senales.py — la cartera de señales: a qué precio estaba cada
empresa cuando los agentes dijeron MUY_INTERESANTE, y cómo evoluciona después.

USO PERSONAL, apagado por defecto (ver activado()): es una ayuda para quien
analiza las empresas y decide por su cuenta, no un resultado del proyecto. No
se muestra en el dashboard ni se descargan precios salvo que se active con
MOSTRAR_SEGUIMIENTO=1 en el .env.

La tesis del proyecto no es de trading: son empresas que, si el análisis acierta,
deberían crecer y multiplicar su valor a lo largo de años. Por eso el horizonte
que se sigue es de 12 meses (y más), y lo que se guarda y se enseña es lo
sencillo: el precio el día de la señal, el precio de hoy, cuánto ha cambiado, el
máximo y el mínimo que tuvo desde entonces y cuánto lleva del año. No hace falta
ser experto en finanzas para leerlo.

Para quien quiera la lectura más exigente (si lo hace mejor o peor que el mercado
en el mismo periodo), también se calcula el exceso sobre el IWM (Russell 2000),
pero es secundario: a unas semanas de una señal no dice casi nada de una tesis a
un año.

Cómo se mide (y los límites):
  - Cada análisis del Detective es una señal: (empresa, modelo, veredicto, fecha).
    Se usa el veredicto REAL del modelo (veredicto_preliminar), no la etiqueta
    ALUCINACION que en el protocolo 1 lo sustituía.
  - La cartera cuenta UNA señal por empresa: la primera vez que algún modelo la
    marcó MUY_INTERESANTE. Los re-análisis posteriores no la duplican.
  - Precio de la señal: el último cierre disponible en el día del análisis o antes.
    Los precios están ajustados por splits y dividendos.
  - Las empresas que dejan de cotizar sin precio en Yahoo no se pueden seguir y
    quedan fuera (sesgo optimista): se cuentan y se avisa.
  - Una señal de hace unos días tiene un recorrido que no significa nada todavía:
    se enseñan los días que lleva para que se vea.

Cómo usarlo:
  python seguimiento_senales.py --actualizar   # baja los precios nuevos
  python seguimiento_senales.py                # imprime la cartera
El orquestador baja los precios al final de cada pasada y el dashboard lo enseña
en la pestaña Seguimiento.
"""

import os
import time
import logging
import argparse
from bisect import bisect_right
from datetime import date, timedelta
from statistics import mean, median

log = logging.getLogger(__name__)


def activado() -> bool:
    """El seguimiento es de uso PERSONAL: enseña cómo se movió el precio de cada
    empresa marcada, y eso no es un resultado del proyecto ni consejo de
    inversión, así que está apagado por defecto. Quien clone el repo, vea una
    demo o despliegue el dashboard no lo ve ni descarga precios. Para usarlo,
    MOSTRAR_SEGUIMIENTO=1 en el .env (que no se sube a git)."""
    return os.getenv("MOSTRAR_SEGUIMIENTO", "0").strip().lower() in ("1", "true", "si", "sí", "yes")

BENCHMARK = "IWM"  # ETF del Russell 2000
PAUSA_ENTRE_TICKERS = 0.6  # misma prudencia con Yahoo que en mercado_yfinance.py

HORIZONTE_DIAS = 365  # el plazo de la tesis: un año

# Horizontes cortos para la lectura avanzada, en días de mercado desde la señal.
HORIZONTES = {"1s": 5, "1m": 21, "3m": 63}
MIN_DIAS_MERCADO_AVANZADO = 5  # menos recorrido que esto no entra en las estadísticas

# Nombres antiguos de los veredictos, por si quedan filas sin migrar.
VEREDICTO_CANONICO = {
    "SEGURO": "MUY_INTERESANTE", "DUDOSO": "INTERESANTE", "HUMO": "NADA_INTERESANTE",
}

DDL = """
create table if not exists precios_seguimiento (
    ticker  varchar(12)   not null,
    fecha   date          not null,
    cierre  decimal(14,4) not null,
    primary key (ticker, fecha)
);
"""


# ---------- cálculo (puro, sin base de datos) ----------

def seguimiento_de_una_senal(fecha_senal: date, precios: list, bench: list = None):
    """
    precios y bench: listas [(fecha, cierre_ajustado)] ordenadas por fecha.
    Devuelve None si no hay ningún precio en el día de la señal o antes. Si no, un
    dict con el precio de la señal, el de hoy, el retorno, el recorrido (máximo y
    mínimo desde la señal) y la serie de precios con base 100 en el día de la señal;
    y, si hay benchmark, el exceso sobre el IWM.
    """
    if not precios:
        return None
    fechas = [f for f, _ in precios]
    i0 = bisect_right(fechas, fecha_senal) - 1  # último cierre en el día de la señal o antes
    if i0 < 0:
        return None

    f0, p0 = precios[i0]
    if not p0:
        return None
    p0 = float(p0)

    fechas_b = [f for f, _ in bench] if bench else []

    def cierre_bench(fecha):
        # El benchmark cotiza los mismos días de mercado; si falta uno, el último conocido.
        j = bisect_right(fechas_b, fecha) - 1
        return float(bench[j][1]) if j >= 0 else None

    b0 = cierre_bench(f0) if bench else None

    serie, curva_exceso = [], []
    for k, (f, p) in enumerate(precios[i0:]):
        ret = float(p) / p0 - 1
        dias = max((f - fecha_senal).days, 0)
        serie.append((dias, 100 * (1 + ret)))
        b = cierre_bench(f) if b0 else None
        if b0 and b is not None:
            curva_exceso.append((k, ret - (b / b0 - 1)))

    f_actual, p_actual = precios[-1]
    p_actual = float(p_actual)
    retornos = [s[1] / 100 - 1 for s in serie]
    resultado = {
        "fecha_referencia": f0,
        "precio_senal": p0,
        "fecha_actual": f_actual,
        "precio_actual": p_actual,
        "multiplo": p_actual / p0,
        "retorno": p_actual / p0 - 1,
        "dias_calendario": max((f_actual - fecha_senal).days, 0),
        "dias_mercado": len(serie) - 1,
        "progreso_anio": min(max((f_actual - fecha_senal).days, 0) / HORIZONTE_DIAS, 1.0),
        "max_subida": max(retornos),
        "max_caida": min(retornos),
        "serie": serie,
        "curva": curva_exceso,
        "retorno_bench": None,
        "exceso": None,
    }
    if b0 and curva_exceso:
        b_actual = cierre_bench(f_actual)
        resultado["retorno_bench"] = b_actual / b0 - 1
        resultado["exceso"] = curva_exceso[-1][1]
    for nombre, dias in HORIZONTES.items():
        punto = next((e for d, e in curva_exceso if d == dias), None)
        resultado[f"exceso_{nombre}"] = punto
    return resultado


def primeras_senales(senales: list, veredicto: str = "MUY_INTERESANTE",
                     modelos=("claude", "gemini")) -> list:
    """
    La cartera: UNA señal por empresa, la primera vez que algún modelo le dio ese
    veredicto. Guarda qué modelos se lo han dado alguna vez (campo "modelos") y si
    los dos (campo "ambos"). Los re-análisis posteriores no duplican la empresa.
    """
    por_empresa = {}
    for s in senales:
        if s["veredicto"] != veredicto:
            continue
        por_empresa.setdefault(s["empresa_id"], []).append(s)

    cartera = []
    for empresa_id, ss in por_empresa.items():
        primera = min(ss, key=lambda s: s["fecha"])
        dieron = sorted({s["modelo"] for s in ss})
        cartera.append({
            "ticker": primera["ticker"], "empresa_id": empresa_id,
            "modelo": "primera señal", "veredicto": veredicto,
            "fecha": primera["fecha"], "modelos": dieron,
            "ambos": all(m in dieron for m in modelos),
        })
    return sorted(cartera, key=lambda c: c["fecha"])


def senales_de_consenso(senales: list, modelos=("claude", "gemini")) -> list:
    """
    Señales a nivel de EMPRESA para la lectura avanzada: toma el último análisis
    de cada uno de los dos modelos y las agrupa en "ambos MUY_INTERESANTE", "uno
    solo" o "ninguno". La fecha es la del más reciente de los dos: es cuando ya
    existía el consenso. Solo empresas con análisis de los dos modelos.
    """
    ultimo = {}
    for s in senales:
        if s["modelo"] not in modelos:
            continue
        clave = (s["empresa_id"], s["modelo"])
        if clave not in ultimo or s["fecha"] > ultimo[clave]["fecha"]:
            ultimo[clave] = s

    por_empresa = {}
    for (empresa_id, modelo), s in ultimo.items():
        por_empresa.setdefault(empresa_id, {})[modelo] = s

    consenso = []
    for empresa_id, d in por_empresa.items():
        if len(d) < len(modelos):
            continue
        muy = sum(1 for s in d.values() if s["veredicto"] == "MUY_INTERESANTE")
        grupo = {2: "ambos MUY_INTERESANTE", 1: "uno solo MUY_INTERESANTE", 0: "ninguno MUY_INTERESANTE"}[muy]
        consenso.append({
            "ticker": next(iter(d.values()))["ticker"],
            "empresa_id": empresa_id,
            "modelo": "consenso",
            "veredicto": grupo,
            "fecha": max(s["fecha"] for s in d.values()),
        })
    return consenso


def seguimiento_de_todas(senales: list, precios_por_ticker: dict) -> tuple:
    """Aplica seguimiento_de_una_senal a cada señal. Devuelve
    (seguimientos, sin_precios, sin_referencia): las que se pudieron medir, las
    de empresas sin precios en Yahoo y las que no tienen precio en su día."""
    bench = precios_por_ticker.get(BENCHMARK, [])
    medidas, sin_precios, sin_referencia = [], [], []
    for s in senales:
        serie = precios_por_ticker.get(s["ticker"])
        if not serie:
            sin_precios.append(s)
            continue
        m = seguimiento_de_una_senal(s["fecha"], serie, bench)
        if m is None:
            sin_referencia.append(s)
        else:
            medidas.append({**s, **m})
    return medidas, sin_precios, sin_referencia


def _con_recorrido(medidas: list) -> list:
    """Para las estadísticas: solo señales con unos días de recorrido y con
    benchmark. Con un día de precio el exceso sería 0 por construcción."""
    return [m for m in medidas
            if m["dias_mercado"] >= MIN_DIAS_MERCADO_AVANZADO and m["exceso"] is not None]


def resumen_por_grupo(medidas: list, clave="veredicto") -> list:
    """Lectura avanzada: una fila por grupo con cuántas señales, cuánto llevan y
    cómo van frente al IWM (media, mediana y % que lo bate), más el exceso a 1
    semana, 1 mes y 3 meses con el número de señales que ya llegaron a ese horizonte."""
    grupos = {}
    for m in _con_recorrido(medidas):
        grupos.setdefault(m[clave], []).append(m)

    filas = []
    for grupo, ms in grupos.items():
        excesos = [m["exceso"] for m in ms]
        fila = {
            clave: grupo,
            "n": len(ms),
            "dias_medios": mean(m["dias_calendario"] for m in ms),
            "exceso_medio": mean(excesos),
            "exceso_mediano": median(excesos),
            "pct_bate_iwm": sum(1 for e in excesos if e > 0) / len(excesos),
            "max_caida_media": mean(m["max_caida"] for m in ms),
        }
        for h in HORIZONTES:
            valores = [m[f"exceso_{h}"] for m in ms if m[f"exceso_{h}"] is not None]
            fila[f"n_{h}"] = len(valores)
            fila[f"exceso_{h}"] = mean(valores) if valores else None
        filas.append(fila)
    return sorted(filas, key=lambda f: -f["n"])


def curva_media_por_grupo(medidas: list, clave="veredicto", min_senales=5) -> list:
    """Lectura avanzada: [(grupo, día, exceso medio, n)] para dibujar el recorrido
    frente al IWM. No se pinta un punto con menos de min_senales señales."""
    puntos = {}
    for m in _con_recorrido(medidas):
        for dia, exceso in m["curva"]:
            puntos.setdefault((m[clave], dia), []).append(exceso)
    return sorted(
        (g, d, mean(v), len(v)) for (g, d), v in puntos.items() if len(v) >= min_senales
    )


# ---------- base de datos ----------

def asegurar_esquema(conn):
    """Crea la tabla de precios si no existe (idempotente). El proyecto no usa
    migraciones (está en el roadmap), así que lo que es nuevo se crea aquí en
    vez de pedir que se aplique a mano."""
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        conn.commit()
    finally:
        cur.close()


def leer_senales(conn) -> list:
    """Una señal por análisis del Detective, con el veredicto real del modelo."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select a.id, a.empresa_id, e.ticker, e.nombre, a.modelo_llm,
                   a.respuesta_llm->>'veredicto_preliminar',
                   a.fecha_analisis::date,
                   coalesce((a.respuesta_llm->>'version_protocolo')::int, 1)
            from auditorias a join empresas e on e.id = a.empresa_id
            where a.respuesta_llm ? 'veredicto_preliminar' and a.veredicto is not null
            order by a.fecha_analisis
            """
        )
        return [
            {"auditoria_id": i, "empresa_id": emp, "ticker": t, "nombre": nombre, "modelo": m,
             "veredicto": VEREDICTO_CANONICO.get(v, v), "fecha": f, "protocolo": p}
            for i, emp, t, nombre, m, v, f, p in cur.fetchall()
        ]
    finally:
        cur.close()


def leer_precios(conn, tickers) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            "select ticker, fecha, cierre from precios_seguimiento "
            "where ticker = any(%s) order by ticker, fecha",
            (list(tickers),),
        )
        precios = {}
        for ticker, fecha, cierre in cur.fetchall():
            precios.setdefault(ticker, []).append((fecha, float(cierre)))
        return precios
    finally:
        cur.close()


def actualizar_precios(conn, senales: list) -> dict:
    """Baja de Yahoo lo que falta de cada ticker con señales (y del IWM), desde
    una semana antes de su primera señal, y lo guarda. Un ticker que falla no
    tumba a los demás. Devuelve {"actualizados": n, "sin_datos": [tickers]}."""
    import yfinance as yf

    primera = {}
    for s in senales:
        primera[s["ticker"]] = min(s["fecha"], primera.get(s["ticker"], s["fecha"]))
    desde_bench = min(primera.values()) if primera else date.today()
    primera[BENCHMARK] = desde_bench

    cur = conn.cursor()
    cur.execute("select ticker, max(fecha) from precios_seguimiento group by ticker")
    ya = dict(cur.fetchall())

    actualizados, sin_datos = 0, []
    for ticker, inicio in primera.items():
        desde = (ya[ticker] + timedelta(days=1)) if ticker in ya else inicio - timedelta(days=7)
        if desde > date.today():
            continue
        try:
            hist = yf.Ticker(ticker).history(start=desde.isoformat(), auto_adjust=True)
        except Exception as e:
            log.warning(f"{ticker}: no pude bajar precios ({e})")
            sin_datos.append(ticker)
            continue
        if hist is None or hist.empty:
            # Sin filas nuevas es normal si ya estaba al día; sin ninguna fila
            # guardada nunca, el ticker no se puede seguir.
            if ticker not in ya:
                sin_datos.append(ticker)
            continue
        filas = [(ticker, idx.date(), round(float(c), 4)) for idx, c in hist["Close"].items() if c == c]
        cur.executemany(
            "insert into precios_seguimiento (ticker, fecha, cierre) values (%s, %s, %s) "
            "on conflict (ticker, fecha) do update set cierre = excluded.cierre",
            filas,
        )
        conn.commit()
        actualizados += 1
        time.sleep(PAUSA_ENTRE_TICKERS)
    cur.close()
    return {"actualizados": actualizados, "sin_datos": sin_datos}


def cargar_seguimiento(conn) -> dict:
    """Todo lo que necesita el dashboard o el resumen, de una vez."""
    asegurar_esquema(conn)
    senales = leer_senales(conn)
    tickers = {s["ticker"] for s in senales} | {BENCHMARK}
    precios = leer_precios(conn, tickers)

    cartera, sin_precios, sin_referencia = seguimiento_de_todas(primeras_senales(senales), precios)
    for c in cartera:
        c["nombre"] = next((s["nombre"] for s in senales if s["empresa_id"] == c["empresa_id"]), None)
    por_senal = seguimiento_de_todas(senales, precios)[0]
    consenso = seguimiento_de_todas(senales_de_consenso(senales), precios)[0]
    hasta = max((serie[-1][0] for serie in precios.values() if serie), default=None)
    return {
        "cartera": cartera, "por_senal": por_senal, "consenso": consenso,
        "sin_precios": sin_precios, "sin_referencia": sin_referencia,
        "precios": {c["ticker"]: precios.get(c["ticker"], []) for c in cartera},
        "bench": precios.get(BENCHMARK, []),
        "precios_hasta": hasta, "n_senales": len(senales),
    }


def fase_seguimiento(conn=None) -> dict:
    """La pasada del orquestador: baja los precios nuevos. Nunca la rompe. Si el
    seguimiento no está activado, no hace nada (ni siquiera llama a Yahoo)."""
    if not activado():
        return {"actualizados": 0, "sin_datos": [], "desactivado": True}
    try:
        from orquestador import conectar_db
        propia = conn is None
        conn = conn or conectar_db()
        try:
            asegurar_esquema(conn)
            resultado = actualizar_precios(conn, leer_senales(conn))
            log.info(f"Seguimiento de señales: {resultado['actualizados']} tickers actualizados, "
                     f"{len(resultado['sin_datos'])} sin datos")
            return resultado
        finally:
            if propia:
                conn.close()
    except Exception as e:
        log.warning(f"Seguimiento de señales: no pude actualizar los precios ({e})")
        return {"actualizados": 0, "sin_datos": []}


def main():
    parser = argparse.ArgumentParser(description="Cartera de señales MUY_INTERESANTE y su evolución")
    parser.add_argument("--actualizar", action="store_true", help="baja primero los precios nuevos de Yahoo")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

    from orquestador import conectar_db
    conn = conectar_db()
    try:
        if args.actualizar:
            fase_seguimiento(conn)
        datos = cargar_seguimiento(conn)
    finally:
        conn.close()

    print(f"\nCartera de MUY_INTERESANTE: {len(datos['cartera'])} empresas "
          f"({len(datos['sin_precios'])} sin precios en Yahoo). Precios hasta {datos['precios_hasta']}.")
    print(f"{'ticker':7} {'señal':11} {'precio señal':>13} {'precio hoy':>11} {'cambio':>8} {'días':>5} {'del año':>8}")
    for c in sorted(datos["cartera"], key=lambda c: c["fecha"]):
        print(f"{c['ticker']:7} {c['fecha']:%d/%m/%Y}  {c['precio_senal']:12.2f} {c['precio_actual']:11.2f} "
              f"{100 * c['retorno']:+7.1f}% {c['dias_calendario']:5d} {100 * c['progreso_anio']:7.0f}%")


if __name__ == "__main__":
    main()
