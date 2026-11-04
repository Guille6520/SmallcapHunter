"""
contexto_sector.py — el contexto de sector de cada candidata, para quien mira el
dashboard: cómo se está moviendo el ETF de su sector frente al Russell 2000 y qué
titulares ha habido del tema en los últimos días.

Es INFORMACIÓN, no análisis. Lo que hago aquí no entra en el score ni lo ven los
Detectives ni el Auditor (son ciegos a propósito, ver sectores.py y el test
tests/test_contexto_sector.py que lo vigila).

Qué guardo (dos tablas, creadas por este módulo):
  - sector_precios: cierres ajustados de los ETFs de sector y del IWM.
  - sector_noticias: titular, fuente, fecha y enlace. NUNCA el texto completo de
    la noticia: solo lo necesario para que el usuario pulse y la lea donde está.

De dónde salen los titulares (dos fuentes, cada una puede fallar sin tumbar la otra):
  - Google News (RSS), buscando por las palabras del sector en la última semana.
    Es gratis y sin clave, y trae titulares generales del tema (puede haber ruido).
    Es un servicio pensado para lectores personales: si esto dejara de ser una
    herramienta de uso propio, habría que pasarse a una API de noticias con licencia.
  - Yahoo Finance, las noticias del ETF del sector. Es la vía no oficial de yfinance
    y muchas veces está caída: si no responde, no pasa nada.
Cada titular lleva su origen para que se vea de dónde viene.

Cómo usarlo:
  python contexto_sector.py              # baja precios y titulares y enseña un resumen
  python contexto_sector.py --sin-noticias   # solo precios
El orquestador lo ejecuta al final de cada pasada (fase_sectores).
"""

import time
import logging
import argparse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import date, datetime, timedelta, timezone

import requests

from sectores import BENCHMARK, SECTORES, etfs_a_seguir, sector_de_sic

log = logging.getLogger(__name__)

PAUSA_ENTRE_ETFS = 0.6          # misma prudencia con Yahoo que el resto del proyecto
PAUSA_BUSQUEDA = 2.0            # entre búsquedas de titulares, para no machacar el servicio
DIAS_HISTORICO_INICIAL = 400    # lo justo para el retorno a 12 meses la primera vez
DIAS_NOTICIAS = 7               # ventana de titulares que busco
MAX_NOTICIAS_POR_ORIGEN = 6
HORAS_ENTRE_BUSQUEDAS = 6       # no vuelvo a buscar noticias de un sector antes de esto

# Horizontes en días de mercado, para el movimiento del sector.
HORIZONTES = {"1 mes": 21, "3 meses": 63, "12 meses": 252}

DDL = """
create table if not exists sector_precios (
    ticker  varchar(12)   not null,
    fecha   date          not null,
    cierre  decimal(14,4) not null,
    primary key (ticker, fecha)
);
create table if not exists sector_noticias (
    id        serial primary key,
    sector    varchar(40)  not null,
    fecha     timestamptz,
    titular   text         not null,
    fuente    varchar(160),
    enlace    text         not null,
    origen    varchar(20)  not null,
    obtenida  timestamptz  not null default now(),
    unique (sector, enlace)
);
create index if not exists sector_noticias_sector_fecha on sector_noticias (sector, fecha desc);
"""


# ---------- cálculo (puro, sin base de datos ni red) ----------

def retornos(serie: list) -> dict:
    """serie: [(fecha, cierre)] ordenada por fecha. Devuelvo el retorno a cada
    horizonte contando días de mercado hacia atrás desde el último cierre. Si no hay
    historia suficiente para un horizonte, ese horizonte queda en None (no invento)."""
    resultado = {nombre: None for nombre in HORIZONTES}
    if not serie:
        return resultado
    ultimo = float(serie[-1][1])
    for nombre, dias in HORIZONTES.items():
        if len(serie) > dias:
            base = float(serie[-1 - dias][1])
            if base:
                resultado[nombre] = ultimo / base - 1
    return resultado


def movimiento_frente_al_benchmark(serie_etf: list, serie_bench: list) -> dict:
    """Para cada horizonte: retorno del ETF, retorno del IWM y la diferencia. Miro
    las dos series en las mismas fechas (las del ETF), para no comparar periodos
    distintos si a una le faltan días."""
    if not serie_etf or not serie_bench:
        return {nombre: {"etf": None, "iwm": None, "dif": None} for nombre in HORIZONTES}
    comunes = {f for f, _ in serie_etf} & {f for f, _ in serie_bench}
    comunes_etf = [(f, c) for f, c in serie_etf if f in comunes]
    comunes_bench = [(f, c) for f, c in serie_bench if f in comunes]
    r_etf, r_bench = retornos(comunes_etf), retornos(comunes_bench)
    return {
        nombre: {
            "etf": r_etf[nombre], "iwm": r_bench[nombre],
            "dif": (r_etf[nombre] - r_bench[nombre]) if r_etf[nombre] is not None and r_bench[nombre] is not None else None,
        }
        for nombre in HORIZONTES
    }


def parsear_google_rss(xml_texto: str, maximo: int = MAX_NOTICIAS_POR_ORIGEN) -> list:
    """Titulares de un RSS de Google News. Me quedo con título, enlace, fuente y
    fecha. Google pega la fuente al final del título ("Titular - Reuters"): se la
    quito del título porque ya la guardo aparte. Descarto lo que no tenga título o
    enlace y lo repetido. Si el texto no es un RSS válido, devuelvo lista vacía."""
    try:
        raiz = ET.fromstring(xml_texto)
    except (ET.ParseError, TypeError):
        return []
    noticias, vistos = [], set()
    for item in raiz.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or "").strip()
        fuente = (item.findtext("source") or "").strip() or None
        if not titulo or not enlace or enlace in vistos:
            continue
        vistos.add(enlace)
        if fuente and titulo.endswith(f" - {fuente}"):
            titulo = titulo[: -len(f" - {fuente}")].rstrip()
        try:
            fecha = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            fecha = None
        noticias.append({"titular": titulo, "enlace": enlace, "fuente": fuente,
                         "fecha": fecha, "origen": "google_news"})
        if len(noticias) >= maximo:
            break
    return noticias


def parsear_yahoo(items: list, maximo: int = MAX_NOTICIAS_POR_ORIGEN) -> list:
    """Titulares de lo que devuelve yfinance (Ticker.news). Tolero los dos formatos
    que ha tenido: el nuevo, con todo dentro de 'content', y el viejo, plano."""
    noticias, vistos = [], set()
    for item in items or []:
        c = item.get("content") if isinstance(item.get("content"), dict) else item
        titulo = (c.get("title") or "").strip()
        enlace = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                  or c.get("link") or "").strip()
        if not titulo or not enlace or enlace in vistos:
            continue
        vistos.add(enlace)
        fecha = None
        if c.get("pubDate"):
            try:
                fecha = datetime.fromisoformat(str(c["pubDate"]).replace("Z", "+00:00"))
            except ValueError:
                fecha = None
        elif c.get("providerPublishTime"):
            fecha = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc)
        fuente = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
        noticias.append({"titular": titulo, "enlace": enlace, "fuente": fuente, "fecha": fecha, "origen": "yahoo"})
        if len(noticias) >= maximo:
            break
    return noticias


# ---------- fuentes de titulares (red; cada una falla sola) ----------

def titulares_yahoo(etf: str) -> list:
    """Las noticias que Yahoo asocia al ETF del sector. Si Yahoo no responde o
    está caído, devuelvo lista vacía y lo dejo en el log."""
    try:
        import yfinance as yf
        return parsear_yahoo(yf.Ticker(etf).news)
    except Exception as e:
        log.warning(f"Noticias de Yahoo para {etf}: no pude leerlas ({str(e)[:120]})")
        return []


def titulares_google(busqueda: str) -> list:
    """Titulares de Google News para la consulta del sector, de los últimos días y en
    inglés. Si el servicio no responde o contesta algo raro, devuelvo lista vacía y
    lo dejo en el log."""
    params = {"q": f"{busqueda} when:{DIAS_NOTICIAS}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    try:
        r = requests.get("https://news.google.com/rss/search", params=params, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 (SmallCapHunter, uso personal)"})
        r.raise_for_status()
        return parsear_google_rss(r.text)
    except Exception as e:
        log.warning(f"Noticias de Google News: no pude leerlas ({str(e)[:120]})")
        return []


# ---------- base de datos ----------

def asegurar_esquema(conn):
    """Creo las tablas si no existen (idempotente). El proyecto no usa migraciones,
    así que lo nuevo se crea aquí en vez de pedir que se aplique a mano."""
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        conn.commit()
    finally:
        cur.close()


def actualizar_precios(conn) -> dict:
    """Bajo de Yahoo lo que falta de cada ETF de sector y del IWM (la primera vez,
    400 días atrás; después, desde el último cierre guardado). Un ETF que falla no
    tumba a los demás."""
    import yfinance as yf

    cur = conn.cursor()
    cur.execute("select ticker, max(fecha) from sector_precios group by ticker")
    ya = dict(cur.fetchall())
    actualizados, sin_datos = 0, []
    for ticker in etfs_a_seguir():
        desde = (ya[ticker] + timedelta(days=1)) if ticker in ya else date.today() - timedelta(days=DIAS_HISTORICO_INICIAL)
        if desde > date.today():
            continue
        try:
            hist = yf.Ticker(ticker).history(start=desde.isoformat(), auto_adjust=True)
        except Exception as e:
            log.warning(f"{ticker}: no pude bajar precios ({str(e)[:120]})")
            sin_datos.append(ticker)
            continue
        if hist is None or hist.empty:
            if ticker not in ya:
                sin_datos.append(ticker)
            continue
        filas = [(ticker, idx.date(), round(float(c), 4)) for idx, c in hist["Close"].items() if c == c]
        cur.executemany(
            "insert into sector_precios (ticker, fecha, cierre) values (%s, %s, %s) "
            "on conflict (ticker, fecha) do update set cierre = excluded.cierre", filas)
        conn.commit()
        actualizados += 1
        time.sleep(PAUSA_ENTRE_ETFS)
    cur.close()
    return {"actualizados": actualizados, "sin_datos": sin_datos}


def sectores_con_candidatas(conn) -> dict:
    """Los sectores que tienen al menos una empresa con score por encima del corte,
    con sus tickers. Son los únicos para los que busco titulares: no gasto
    peticiones en sectores donde no hay nada que mirar."""
    cur = conn.cursor()
    try:
        cur.execute("select valor from configuracion where clave = 'score_minimo_llm'")
        fila = cur.fetchone()
        corte = int(fila[0]) if fila else 25
        cur.execute(
            """
            select e.ticker, e.sic, a.score_total
            from empresas e join auditorias a on a.empresa_id = e.id and a.veredicto is null
            where e.estado = 'scoring_ok' and a.score_total >= %s
            """, (corte,))
        por_sector = {}
        for ticker, sic, score in cur.fetchall():
            por_sector.setdefault(sector_de_sic(sic), []).append((ticker, score))
        return por_sector
    finally:
        cur.close()


def _guardar_noticias(conn, sector: str, noticias: list) -> int:
    cur = conn.cursor()
    try:
        for n in noticias:
            cur.execute(
                "insert into sector_noticias (sector, fecha, titular, fuente, enlace, origen) "
                "values (%s, %s, %s, %s, %s, %s) on conflict (sector, enlace) do nothing",
                (sector, n["fecha"], n["titular"], n["fuente"], n["enlace"], n["origen"]))
        conn.commit()
        return len(noticias)
    finally:
        cur.close()


def _buscado_hace_poco(conn, sector: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("select max(obtenida) from sector_noticias where sector = %s", (sector,))
        ultima = cur.fetchone()[0]
    finally:
        cur.close()
    return ultima is not None and datetime.now(timezone.utc) - ultima < timedelta(hours=HORAS_ENTRE_BUSQUEDAS)


def actualizar_noticias(conn, sectores: list) -> dict:
    """Busco titulares de los sectores dados, de las dos fuentes. Si un sector ya se
    buscó hace menos de HORAS_ENTRE_BUSQUEDAS, lo salto. Si las dos fuentes fallan,
    el sector se queda con lo que ya tuviera."""
    nuevas, saltados, ultima_busqueda = 0, 0, 0.0
    for clave in sectores:
        datos = SECTORES.get(clave)
        if not datos or not datos["busqueda"]:
            continue
        if _buscado_hace_poco(conn, clave):
            saltados += 1
            continue
        noticias = titulares_yahoo(datos["etf"]) if datos["etf"] else []
        espera = PAUSA_BUSQUEDA - (time.time() - ultima_busqueda)
        if espera > 0:
            time.sleep(espera)
        noticias += titulares_google(datos["busqueda"])
        ultima_busqueda = time.time()
        nuevas += _guardar_noticias(conn, clave, noticias)
    return {"guardadas": nuevas, "saltados": saltados}


def leer_panorama(conn) -> list:
    """Todo lo que necesita el dashboard: por cada sector con candidatas, el
    movimiento del ETF frente al IWM, las candidatas y los titulares recientes."""
    asegurar_esquema(conn)
    por_sector = sectores_con_candidatas(conn)
    cur = conn.cursor()
    try:
        cur.execute("select ticker, fecha, cierre from sector_precios order by ticker, fecha")
        precios = {}
        for t, f, c in cur.fetchall():
            precios.setdefault(t, []).append((f, float(c)))

        panorama = []
        for clave, candidatas in sorted(por_sector.items(), key=lambda kv: -len(kv[1])):
            datos = SECTORES[clave]
            cur.execute(
                "select fecha, titular, fuente, enlace, origen from sector_noticias "
                "where sector = %s order by fecha desc nulls last limit 12", (clave,))
            titulares = [{"fecha": f, "titular": t, "fuente": s, "enlace": e, "origen": o}
                         for f, t, s, e, o in cur.fetchall()]
            panorama.append({
                "clave": clave, "nombre": datos["nombre"], "etf": datos["etf"],
                "candidatas": sorted(candidatas, key=lambda x: -x[1]),
                "movimiento": movimiento_frente_al_benchmark(precios.get(datos["etf"], []), precios.get(BENCHMARK, []))
                if datos["etf"] else None,
                "precios_hasta": precios[datos["etf"]][-1][0] if datos["etf"] and precios.get(datos["etf"]) else None,
                "titulares": titulares,
            })
        return panorama
    finally:
        cur.close()


def fase_sectores(conn=None, con_noticias: bool = True) -> dict:
    """La parte del orquestador: bajo precios de ETFs y titulares de los sectores con
    candidatas. Nunca rompe la pasada: lo que falle se queda en el log."""
    try:
        from orquestador import conectar_db
        propia = conn is None
        conn = conn or conectar_db()
        try:
            asegurar_esquema(conn)
            resultado = {"precios": actualizar_precios(conn)}
            if con_noticias:
                resultado["noticias"] = actualizar_noticias(conn, list(sectores_con_candidatas(conn)))
            log.info(f"Contexto de sector: {resultado}")
            return resultado
        finally:
            if propia:
                conn.close()
    except Exception as e:
        log.warning(f"Contexto de sector: no pude actualizarlo ({e})")
        return {}


def main():
    parser = argparse.ArgumentParser(description="Contexto de sector para el dashboard (precios de ETFs y titulares)")
    parser.add_argument("--sin-noticias", action="store_true", help="solo bajo precios, sin buscar titulares")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

    from orquestador import conectar_db
    conn = conectar_db()
    try:
        fase_sectores(conn, con_noticias=not args.sin_noticias)
        panorama = leer_panorama(conn)
    finally:
        conn.close()

    for s in panorama:
        mov = s["movimiento"]
        linea = ""
        if mov and mov["3 meses"]["etf"] is not None:
            linea = f" | {s['etf']} a 3 meses {100 * mov['3 meses']['etf']:+.1f}% (IWM {100 * mov['3 meses']['iwm']:+.1f}%)"
        print(f"{s['nombre']}: {len(s['candidatas'])} candidatas{linea} | {len(s['titulares'])} titulares")


if __name__ == "__main__":
    main()
