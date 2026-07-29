"""
backtest_capa3.py — el experimento que le faltaba al proyecto: ¿la capa
LLM añade valor sobre la señal numérica, o solo decora?

El backtest de la señal cruda (backtest_rendimiento.py) ya dio su
respuesta incómoda: el cluster de insiders, solo, no bate al Russell.
Este script mide lo que viene después: si el veredicto del agente
(MUY_INTERESANTE / INTERESANTE / NADA_INTERESANTE) DISCRIMINA — si los
MUY_INTERESANTE históricos rindieron mejor que los NADA_INTERESANTE y
que el benchmark. Si discrimina, el sistema de capas se justifica
empíricamente. Si no, también quiero saberlo.

Cómo evito hacerme trampas:
  1. CIEGO. El LLM recibe el 10-Q DE LA ÉPOCA del cluster con la empresa
     anonimizada: nombre y ticker sustituidos, insiders reducidos a su
     cargo, y TODOS los años del texto enmascarados (20XX). Sin esto, el
     modelo "predice" el pasado porque se sabe la historia de la empresa.
  2. Sin información posterior. Solo el texto de la época y las compras
     del cluster hasta la fecha de detección. Nada de 8-K modernos,
     short interest actual ni señales de hoy.
  3. Entrada realista. El retorno se mide desde 5 días hábiles después
     de la detección, con precios ajustados por splits/dividendos, y
     siempre contra el IWM (Russell 2000) en el MISMO periodo.

Limitaciones que asumo y documento (no las escondo):
  - La anonimización no es perfecta: un texto puede delatar el sector o
    la época por el contenido. Es el mismo problema que tiene cualquier
    backtest con LLMs y lo declaro en la memoria.
  - Las deslistadas sin precios en Yahoo quedan excluidas y eso mete
    sesgo optimista — el resumen final dice cuántas fueron.

Requiere haber ejecutado antes:
  python validacion_historica.py        (genera validadas_historicas.csv)
  python ingesta_10q_historico.py --muestra 40   (trae los 10-Q de la época)

Cómo usarlo:
  python backtest_capa3.py --muestra 30
  python backtest_capa3.py --muestra 30 --modelo claude
  python backtest_capa3.py --ticker BE --modelo groq   # un caso, para depurar
"""

import os
import re
import csv
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, date
from statistics import median

import psycopg2
import yfinance as yf

from detective import llamar_modelo, modelo_secundario, parsear_json_llm
from filtro_capa1 import es_cargo_csuite

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

ARCHIVO_CASOS = "validadas_historicas.csv"
ARCHIVO_RESULTADOS = "resultados_backtest_capa3.csv"

MAX_CARACTERES_MDA_HISTORICO = 12000
DIAS_HABILES_ENTRADA = 5
PAUSA_ENTRE_TICKERS = 0.6

# Sufijos legales que quito del nombre antes de buscarlo en el texto —
# "Bloom Energy Corporation" aparece como "Bloom Energy" mil veces.
SUFIJOS_LEGALES = re.compile(
    r",?\s*(incorporated|corporation|corp\.?|inc\.?|ltd\.?|llc|plc|"
    r"holdings?|group|company|co\.)\s*$",
    re.IGNORECASE
)


def anonimizar_texto(texto: str, nombre: str, ticker: str) -> str:
    """
    Borro del texto todo lo que identifica a la empresa o la época:
    nombre (con y sin sufijo legal), ticker, y cualquier año de cuatro
    cifras. El objetivo no es anonimización forense — es quitarle al
    LLM los asideros obvios para que evalúe el NEGOCIO, no su memoria
    de lo que le pasó después a esa empresa.
    """
    resultado = texto

    nombre_base = SUFIJOS_LEGALES.sub("", (nombre or "").strip())
    for variante in (nombre, nombre_base):
        if variante and len(variante) >= 4:
            resultado = re.sub(re.escape(variante), "la Empresa",
                               resultado, flags=re.IGNORECASE)

    if ticker and len(ticker) >= 2:
        resultado = re.sub(rf"\b{re.escape(ticker)}\b", "XXXX", resultado)

    # Años enmascarados: 1990-2099 -> 20XX. Los importes y porcentajes
    # se quedan — son la sustancia del análisis.
    resultado = re.sub(r"\b(19\d{2}|20\d{2})\b", "20XX", resultado)

    return resultado


def serie_financiera_epoca(conn, empresa_id: int, fecha_limite) -> str:
    """
    Los números XBRL que existían EN LA ÉPOCA del cluster: hasta 8
    trimestres anteriores a la detección, etiquetados en relativo
    (T-7...T-0) para no delatar el año. Con esto el test ciego deja de
    evaluar solo la capa de texto y pasa a probar lo que el sistema
    real hace: números + narrativa juntos.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select fecha_fin, revenue, gross_profit, operating_cash_flow
            from metricas_trimestrales
            where empresa_id = %s and fecha_fin <= %s and revenue is not null
            order by fecha_fin desc limit 8
            """,
            (empresa_id, fecha_limite)
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    if not filas:
        return "  (sin serie financiera disponible)"

    filas = list(reversed(filas))  # del más antiguo al más reciente
    lineas = []
    for i, (_, revenue, gross, ocf) in enumerate(filas):
        etiqueta = f"T-{len(filas) - 1 - i}"
        partes = [f"revenue ${revenue/1e6:,.1f}M"]
        if gross is not None and revenue:
            partes.append(f"margen bruto {gross/revenue*100:.0f}%")
        if ocf is not None:
            partes.append(f"caja operativa ${ocf/1e6:+,.1f}M")
        lineas.append(f"  {etiqueta}: " + " | ".join(partes))
    return "\n".join(lineas)


def construir_prompt_historico(texto_anonimo: str, resumen_cluster: str,
                               serie_financiera: str) -> str:
    """
    La versión enriquecida: texto de la época + serie numérica de la
    época. Sigue sin haber 8-K ni señales modernas (no existen para el
    pasado), pero ya no es solo la capa de texto aislada — es lo más
    parecido al sistema completo que se puede reconstruir hacia atrás.
    """
    return f"""Eres un analista financiero escéptico. Te doy datos de una small cap americana ANONIMIZADA — no sabes cuál es ni de qué año son los datos, y no debes intentar adivinarlo. Evalúa solo lo que lees.

Contexto: un grupo de directivos de esta empresa compró acciones con su propio dinero en mercado abierto en una ventana reciente de 60 días:
{resumen_cluster}

SERIE TRIMESTRAL de la época (T-0 es el trimestre más reciente antes de las compras):
{serie_financiera}

TEXTO MD&A del último trimestre (anonimizado, puede estar recortado):
\"\"\"
{texto_anonimo}
\"\"\"

Tu tarea:
1. ¿La TASA de crecimiento está acelerando trimestre a trimestre? (no basta con que crezca)
2. ¿Los márgenes y la caja operativa mejoran? ¿El texto sugiere un catalizador no obvio?
3. ¿O las compras de los directivos parecen injustificadas por los fundamentales?
4. Da un veredicto de interés para investigación: MUY_INTERESANTE (señales claras), INTERESANTE (mixto), o NADA_INTERESANTE (los fundamentales no acompañan)

Responde ÚNICAMENTE con un JSON válido:
{{
  "veredicto": "MUY_INTERESANTE|INTERESANTE|NADA_INTERESANTE",
  "razon_principal": "una frase"
}}"""


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def cargar_casos(ruta: str, ticker_filtro: str = None) -> list:
    """
    Acepto dos formatos de entrada:
      - validadas_historicas.csv (empresa_id, ticker, fecha_deteccion)
      - resultados_test_ganadoras.csv (ticker, fecha_cluster, tuvo_cluster...)
        — de ahí tomo solo las ganadoras CON señal, con fecha_cluster
        como fecha de detección. Así el mismo experimento responde
        "¿los LLM habrían aconsejado a las ganadoras reales?"
    El empresa_id que falte se resuelve después por ticker contra la BD.
    """
    casos = []
    with open(ruta, encoding="utf-8") as f:
        lector = csv.DictReader(f)
        es_ganadoras = "fecha_cluster" in (lector.fieldnames or [])
        for fila in lector:
            if ticker_filtro and fila["ticker"] != ticker_filtro:
                continue
            if es_ganadoras:
                if str(fila.get("tuvo_cluster", "")).lower() not in ("true", "1"):
                    continue
                if not fila.get("fecha_cluster"):
                    continue
                fecha = fila["fecha_cluster"]
            else:
                fecha = fila["fecha_deteccion"]
            casos.append({
                "empresa_id": int(fila["empresa_id"]) if fila.get("empresa_id") else None,
                "ticker": fila["ticker"],
                "fecha_deteccion": datetime.strptime(fecha, "%Y-%m-%d").date(),
            })
    return casos


def resolver_empresa_id(conn, casos: list) -> list:
    """Relleno los empresa_id que el CSV no traía, buscando por ticker."""
    cur = conn.cursor()
    try:
        for caso in casos:
            if caso["empresa_id"] is None:
                cur.execute("select id from empresas where ticker = %s",
                            (caso["ticker"],))
                fila = cur.fetchone()
                caso["empresa_id"] = fila[0] if fila else None
    finally:
        cur.close()
    return [c for c in casos if c["empresa_id"] is not None]


def obtener_caso_completo(conn, caso: dict):
    """
    El material de la época: nombre (para anonimizar), el 10-Q anterior
    a la detección, y las compras del cluster. Devuelvo None si falta
    el texto — significa que ingesta_10q_historico no cubrió este caso.
    """
    cur = conn.cursor()
    try:
        cur.execute("select nombre from empresas where id = %s", (caso["empresa_id"],))
        fila = cur.fetchone()
        nombre = fila[0] if fila else ""

        cur.execute(
            """
            select texto_mda, fecha_fin
            from metricas_trimestrales
            where empresa_id = %s and fecha_fin <= %s and texto_mda is not null
            order by fecha_fin desc limit 1
            """,
            (caso["empresa_id"], caso["fecha_deteccion"])
        )
        fila = cur.fetchone()
        if not fila:
            return None
        texto, fecha_fin = fila

        # Un texto de hace más de un año respecto a la detección no es
        # "de la época" — mejor excluir el caso que analizar un informe
        # que no refleja el momento del cluster.
        if (caso["fecha_deteccion"] - fecha_fin).days > 365:
            return None

        cur.execute(
            """
            select cargo, fecha_transaccion, importe_total
            from insider_transactions
            where empresa_id = %s and tipo_transaccion = 'P'
              and fecha_transaccion between %s and %s
            order by fecha_transaccion
            """,
            (caso["empresa_id"],
             caso["fecha_deteccion"] - timedelta(days=60),
             caso["fecha_deteccion"])
        )
        compras = cur.fetchall()

        return {"nombre": nombre, "texto": texto, "compras": compras}
    finally:
        cur.close()


def pasa_embudo_historico(conn, caso: dict, compras: list) -> tuple:
    """
    El filtro que le faltaba al experimento: en producción, el LLM solo
    ve empresas que pasaron la Capa 1 (C-suite en el cluster) y la
    Capa 2 (aceleración). Las validadas históricas solo pasaron el
    cluster + tamaño — evaluar al agente sobre ese charco sin filtrar
    es preguntarle por empresas que el sistema jamás le enseñaría.

    Dos condiciones mínimas, con los datos de la ÉPOCA:
      1. Algún comprador del cluster es C-suite (misma función de
         producción, no una versión suave).
      2. La tasa de crecimiento del revenue ACELERA en los últimos
         trimestres disponibles antes de la detección.
    Devuelvo (pasa, motivo_si_no).
    """
    if not any(es_cargo_csuite(cargo or "") for cargo, _, _ in compras):
        return False, "sin_csuite"

    cur = conn.cursor()
    try:
        cur.execute(
            """
            select revenue from metricas_trimestrales
            where empresa_id = %s and fecha_fin <= %s and revenue is not null
              and revenue > 0
            order by fecha_fin desc limit 5
            """,
            (caso["empresa_id"], caso["fecha_deteccion"])
        )
        revenues = [float(f[0]) for f in reversed(cur.fetchall())]
    finally:
        cur.close()

    if len(revenues) < 4:
        return False, "sin_serie_suficiente"

    tasas = [revenues[i] / revenues[i - 1] - 1 for i in range(1, len(revenues))]
    # Aceleración mínima: la última tasa supera a la anterior. Es la
    # versión binaria del score_temporal de producción — un corte, no
    # el score fino, pero el MISMO concepto.
    if tasas[-1] <= tasas[-2]:
        return False, "sin_aceleracion"

    return True, ""


def resumen_cluster_anonimo(compras: list) -> str:
    """Los insiders reducidos a su cargo — el nombre propio identifica."""
    lineas = []
    for i, (cargo, fecha, importe) in enumerate(compras):
        importe_txt = f"${importe:,.0f}" if importe else "importe no disponible"
        lineas.append(f"  - Insider {chr(65 + i % 26)} ({cargo or 'cargo no especificado'}): {importe_txt}")
    return "\n".join(lineas) or "  (sin detalle de compras)"


def retornos_vs_benchmark(ticker: str, fecha_deteccion: date):
    """
    Retorno a 12 y 24 meses desde la entrada realista, y el del IWM en
    el mismo periodo exacto. Devuelvo dict con Nones donde no hay datos
    (deslistada, o caso demasiado reciente para el horizonte).
    """
    entrada = fecha_deteccion + timedelta(days=DIAS_HABILES_ENTRADA + 2)
    fin = entrada + timedelta(days=740)

    def serie(simbolo):
        try:
            datos = yf.download(simbolo, start=entrada.isoformat(),
                                end=fin.isoformat(), auto_adjust=True,
                                progress=False)
            if datos is None or datos.empty:
                return None
            cierres = datos["Close"]
            # yfinance a veces devuelve columnas multi-nivel
            if hasattr(cierres, "columns"):
                cierres = cierres.iloc[:, 0]
            return cierres.dropna()
        except Exception as e:
            log.warning(f"{simbolo}: error de descarga ({e})")
            return None

    precios = serie(ticker)
    bench = serie("IWM")
    if precios is None or len(precios) < 10 or bench is None:
        return None

    def retorno_a(dias, cierres):
        objetivo = entrada + timedelta(days=dias)
        posteriores = cierres[cierres.index.date >= entrada]
        if len(posteriores) == 0:
            return None
        inicio = float(posteriores.iloc[0])
        ventana = cierres[cierres.index.date <= objetivo]
        if len(ventana) == 0:
            return None
        # Si el último dato está muy lejos del objetivo, el horizonte
        # no se alcanzó (deslistada a medias o caso reciente).
        if (objetivo - ventana.index[-1].date()).days > 30:
            return None
        return float(ventana.iloc[-1]) / inicio - 1

    return {
        "retorno_12m": retorno_a(365, precios),
        "retorno_24m": retorno_a(730, precios),
        "bench_12m": retorno_a(365, bench),
        "bench_24m": retorno_a(730, bench),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Backtest ciego de la Capa 3: ¿el veredicto LLM discrimina?"
    )
    parser.add_argument("--muestra", type=int, default=30,
                        help="Cuántos casos históricos analizar")
    parser.add_argument("--modelo", choices=["groq", "gemini", "claude"],
                        default=None,
                        help="Modelo a evaluar (por defecto, el secundario del .env)")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Un solo caso, para depurar")
    parser.add_argument("--pausa", type=int, default=5,
                        help="Segundos entre llamadas LLM (sube a 30+ si usas gemini)")
    parser.add_argument("--archivo", type=str, default=ARCHIVO_CASOS,
                        help="CSV de casos: validadas_historicas.csv o "
                             "resultados_test_ganadoras.csv (solo las CON señal)")
    parser.add_argument("--sin-filtros", action="store_true",
                        help="Salta el embudo histórico (Capa 1+2) — para comparar "
                             "el agente solo contra el sistema completo")
    args = parser.parse_args()

    modelo = args.modelo or modelo_secundario()
    log.info(f"Modelo evaluado: {modelo}")

    casos = cargar_casos(args.archivo, args.ticker)
    log.info(f"Casos en {args.archivo}: {len(casos)}")

    conn = conectar_db()
    casos = resolver_empresa_id(conn, casos)
    log.info(f"Casos con empresa localizada en la BD: {len(casos)}")
    resultados = []
    sin_texto, sin_precios, errores_llm = 0, 0, 0
    filtradas = {"sin_csuite": 0, "sin_aceleracion": 0, "sin_serie_suficiente": 0}

    try:
        for caso in casos:
            if len(resultados) >= args.muestra:
                break

            material = obtener_caso_completo(conn, caso)
            if material is None:
                sin_texto += 1
                continue

            # El embudo histórico: en producción el LLM solo ve
            # supervivientes de Capa 1+2. Aquí, igual.
            if not args.sin_filtros:
                pasa, motivo = pasa_embudo_historico(conn, caso, material["compras"])
                if not pasa:
                    filtradas[motivo] = filtradas.get(motivo, 0) + 1
                    continue

            texto_anonimo = anonimizar_texto(
                material["texto"], material["nombre"], caso["ticker"]
            )[:MAX_CARACTERES_MDA_HISTORICO]
            resumen = resumen_cluster_anonimo(material["compras"])
            serie = serie_financiera_epoca(conn, caso["empresa_id"],
                                           caso["fecha_deteccion"])

            prompt = construir_prompt_historico(texto_anonimo, resumen, serie)

            try:
                respuesta = parsear_json_llm(llamar_modelo(modelo, prompt))
                veredicto = respuesta.get("veredicto", "").upper().replace(" ", "_")
                if veredicto not in ("MUY_INTERESANTE", "INTERESANTE", "NADA_INTERESANTE"):
                    raise ValueError(f"veredicto raro: {veredicto!r}")
            except Exception as e:
                log.warning(f"{caso['ticker']}: fallo LLM ({e})")
                errores_llm += 1
                time.sleep(args.pausa)
                continue

            time.sleep(args.pausa)

            rets = retornos_vs_benchmark(caso["ticker"], caso["fecha_deteccion"])
            time.sleep(PAUSA_ENTRE_TICKERS)
            if rets is None:
                sin_precios += 1
                log.info(f"{caso['ticker']}: veredicto {veredicto}, sin precios — excluida")
                continue

            exceso_12 = (rets["retorno_12m"] - rets["bench_12m"]
                         if rets["retorno_12m"] is not None and rets["bench_12m"] is not None
                         else None)
            exceso_24 = (rets["retorno_24m"] - rets["bench_24m"]
                         if rets["retorno_24m"] is not None and rets["bench_24m"] is not None
                         else None)

            resultados.append({
                "ticker": caso["ticker"],
                "fecha_deteccion": caso["fecha_deteccion"].isoformat(),
                "veredicto": veredicto,
                "razon": respuesta.get("razon_principal", ""),
                "retorno_12m": rets["retorno_12m"],
                "exceso_12m": exceso_12,
                "exceso_24m": exceso_24,
            })
            log.info(
                f"{caso['ticker']} ({caso['fecha_deteccion']}): {veredicto} | "
                f"exceso 12m: {f'{exceso_12:+.1%}' if exceso_12 is not None else 'n/d'}"
            )
    finally:
        conn.close()

    if not resultados:
        log.error(
            "Sin resultados. ¿Ejecutaste ingesta_10q_historico.py antes? "
            f"(casos sin texto de la época: {sin_texto})"
        )
        return

    with open(ARCHIVO_RESULTADOS, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=list(resultados[0].keys()))
        escritor.writeheader()
        escritor.writerows(resultados)

    # El resumen que importa: ¿el veredicto discrimina?
    print("\n" + "=" * 62)
    print(f"BACKTEST CIEGO DEL SISTEMA — modelo: {modelo}"
          + ("  (SIN embudo histórico)" if args.sin_filtros else "  (CON embudo Capa 1+2)"))
    total_filtradas = sum(filtradas.values())
    print(f"Casos analizados: {len(resultados)}  |  sin texto de época: {sin_texto}"
          f"  |  sin precios (SESGO: excluidas): {sin_precios}  |  fallos LLM: {errores_llm}")
    if not args.sin_filtros:
        print(f"Descartadas por el embudo histórico: {total_filtradas} "
              f"(sin C-suite: {filtradas['sin_csuite']}, sin aceleración: "
              f"{filtradas['sin_aceleracion']}, serie insuficiente: "
              f"{filtradas['sin_serie_suficiente']})")
    print("=" * 62)
    print(f"{'Veredicto':<10} {'n':>4} {'mediana exceso 12m':>20} {'mediana exceso 24m':>20}")

    for v in ("MUY_INTERESANTE", "INTERESANTE", "NADA_INTERESANTE"):
        grupo = [r for r in resultados if r["veredicto"] == v]
        e12 = [r["exceso_12m"] for r in grupo if r["exceso_12m"] is not None]
        e24 = [r["exceso_24m"] for r in grupo if r["exceso_24m"] is not None]
        m12 = f"{median(e12):+.1%}" if e12 else "n/d"
        m24 = f"{median(e24):+.1%}" if e24 else "n/d"
        print(f"{v:<10} {len(grupo):>4} {m12:>20} {m24:>20}")

    print("=" * 62)
    print(f"Detalle por caso en {ARCHIVO_RESULTADOS}")
    print("La pregunta a responder: ¿MUY_INTERESANTE > INTERESANTE >")
    print("NADA_INTERESANTE en exceso de retorno? Si sí, la capa LLM")
    print("discrimina y el embudo se justifica. Si no, también es un")
    print("resultado — y va a la memoria igual.")


if __name__ == "__main__":
    main()
