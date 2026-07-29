"""
backtest_rendimiento.py — mido si la señal de cluster de insiders
batió al mercado en los 12 y 24 meses siguientes a cada detección.

Esto responde a la pregunta final de la hipótesis del proyecto:
¿las empresas que mostraron el patrón realmente subieron más que el
conjunto de small caps, o el patrón no predice nada?

Cuatro decisiones metodológicas, tomadas a propósito para no engañarme:

  1. PUNTO DE ENTRADA REALISTA. No compro el día del cluster — en la
     vida real el Form 4 se conoce con unos días de desfase. Entro 5
     días hábiles después de la fecha de detección. Comprar el día
     exacto sería hacer trampa con información que aún no era pública.
     (La fecha de detección es ahora el día en que el N-ésimo insider
     compró — antes era el final teórico de la ventana de 60 días, lo
     que retrasaba la entrada hasta 2 meses sin motivo.)

  2. PRECIOS AJUSTADOS PARA LOS RETORNOS. Aquí uso auto_adjust=True,
     al contrario que en validacion_historica.py. No es una
     contradicción: para reconstruir el market cap del momento
     necesito el precio SIN ajustar (consistente con las acciones en
     circulación de la época), pero para medir la rentabilidad de
     comprar y vender necesito el precio ajustado por splits y
     dividendos — si no, un split 10:1 dentro del horizonte aparece
     como una caída del 90% que nunca existió.

  3. DESLISTADAS EXCLUIDAS, PERO AVISADO. Excluir las empresas que
     desaparecieron es lo más simple, pero mete sesgo optimista (las
     que quebraron no cuentan sus pérdidas). Por eso el resultado
     AVISA en grande cuántas excluí y qué porcentaje son — el número
     final lleva su asterisco bien visible, no escondido. La versión
     correcta (asignar a cada deslistada su retorno real hasta el
     último día de cotización) queda como mejora pendiente.

  4. BENCHMARK: RUSSELL 2000 (vía ETF IWM). "Subió un 40%" no dice nada
     si el mercado entero subió 35%. Comparo cada empresa contra lo que
     hizo el IWM en su mismo periodo exacto, y lo que me importa es el
     exceso de retorno (empresa - benchmark), no el retorno bruto.

Es un script de solo lectura. No toca ninguna tabla.

Cómo usarlo:
  python backtest_rendimiento.py
  python backtest_rendimiento.py --limite 50   # para probar rápido
"""

import os
import time
import logging
import argparse
from datetime import timedelta

import yfinance as yf
import psycopg2

from filtro_capa1 import detectar_cluster_buying, leer_configuracion
from descubrir_clusters_historicos import obtener_todas_las_compras, obtener_info_empresas
from validacion_historica import (
    obtener_shares_outstanding_historico, obtener_precio_historico
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

PAUSA_ENTRE_TICKERS = 0.6

# Días hábiles de desfase entre la fecha del cluster y mi entrada real.
# Modela que el Form 4 tarda en conocerse y digerirse.
DIAS_DESFASE_ENTRADA = 5

# El ETF que uso como proxy del Russell 2000. Es el estándar de la
# industria para benchmarking de small caps.
TICKER_BENCHMARK = "IWM"


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def precio_ajustado_en_fecha(ticker: str, fecha_objetivo):
    """
    Precio de cierre AJUSTADO por splits y dividendos en la primera
    fecha de cotización igual o posterior a fecha_objetivo. Uso >= aquí
    (no <= como en la validación) porque para medir rentabilidad quiero
    el primer día en que REALMENTE podría haber comprado o vendido en o
    después de la fecha objetivo.

    auto_adjust=True a propósito: el retorno entre dos precios ajustados
    es el retorno real del accionista. Con precios sin ajustar, un split
    dentro del periodo destroza la medida (un 10:1 parece un -90%).
    """
    try:
        t = yf.Ticker(ticker)
        inicio = fecha_objetivo.isoformat()
        fin = (fecha_objetivo + timedelta(days=12)).isoformat()
        hist = t.history(start=inicio, end=fin, auto_adjust=True)
        if hist.empty:
            return None
        hist = hist.reset_index()
        hist["Date"] = hist["Date"].dt.date
        posteriores = hist[hist["Date"] >= fecha_objetivo]
        if posteriores.empty:
            return None
        return float(posteriores.iloc[0]["Close"])
    except Exception as e:
        log.warning(f"Sin precio para {ticker} cerca de {fecha_objetivo}: {e}")
        return None


def retorno_periodo(ticker: str, fecha_entrada, meses: int):
    """
    Retorno porcentual de comprar en fecha_entrada y vender N meses
    después. Devuelvo None si falta cualquiera de los dos precios —
    típicamente porque la empresa se deslistó antes de la fecha de
    salida (esas las contamos aparte, no como cero).
    """
    fecha_salida = fecha_entrada + timedelta(days=int(meses * 30.44))

    precio_entrada = precio_ajustado_en_fecha(ticker, fecha_entrada)
    if precio_entrada is None or precio_entrada <= 0:
        return None

    precio_salida = precio_ajustado_en_fecha(ticker, fecha_salida)
    if precio_salida is None or precio_salida <= 0:
        return None

    return (precio_salida - precio_entrada) / precio_entrada


# Cacheo los retornos del benchmark para no pedir el IWM a yfinance una
# y otra vez para las mismas fechas — muchas empresas comparten periodos
# solapados y sería un desperdicio de llamadas.
_cache_benchmark = {}

def retorno_benchmark(fecha_entrada, meses: int):
    clave = (fecha_entrada, meses)
    if clave in _cache_benchmark:
        return _cache_benchmark[clave]
    resultado = retorno_periodo(TICKER_BENCHMARK, fecha_entrada, meses)
    _cache_benchmark[clave] = resultado
    return resultado


def main():
    parser = argparse.ArgumentParser(
        description="Backtest de rendimiento de la señal de cluster vs Russell 2000"
    )
    parser.add_argument("--limite", type=int, help="Procesa solo las primeras N (para probar)")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion(conn)
        dias_ventana = int(config.get("dias_ventana_cluster", 60))
        min_insiders = int(config.get("min_insiders_cluster", 3))
        cap_min = int(config.get("market_cap_min", 50_000_000))
        cap_max = int(config.get("market_cap_max", 2_000_000_000))

        log.info("Recalculando clusters y validando tamaño histórico...")
        compras_por_empresa = obtener_todas_las_compras(conn)
        info_empresas = obtener_info_empresas(conn)

        # Reconstruyo la misma lista de validadas que la validación histórica:
        # solo empresas que de verdad eran small/mid cap en el momento.
        candidatos = []
        for empresa_id, transacciones in compras_por_empresa.items():
            if len(transacciones) < min_insiders:
                continue
            resultado = detectar_cluster_buying(transacciones, dias_ventana, min_insiders)
            if not resultado["cumple"] or not resultado["fecha_deteccion"]:
                continue
            info = info_empresas.get(empresa_id)
            if not info or not info[0]:
                continue
            fecha_det = resultado["fecha_deteccion"]
            shares = obtener_shares_outstanding_historico(conn, empresa_id, fecha_det)
            precio = obtener_precio_historico(info[0], fecha_det)
            if shares and precio:
                mc = shares * precio
                if cap_min <= mc <= cap_max:
                    candidatos.append((info[0], fecha_det))

        if args.limite:
            candidatos = candidatos[:args.limite]

        log.info(f"Candidatas validadas a analizar: {len(candidatos)}")

        # Acumuladores por horizonte
        resultados = {12: [], 24: []}
        excluidas_deslistadas = {12: 0, 24: 0}

        for i, (ticker, fecha_det) in enumerate(candidatos, 1):
            fecha_entrada = fecha_det + timedelta(days=DIAS_DESFASE_ENTRADA)

            for meses in (12, 24):
                ret_empresa = retorno_periodo(ticker, fecha_entrada, meses)
                if ret_empresa is None:
                    # No hay precio de salida: la empresa se deslistó antes
                    # de cumplir el horizonte. De momento las excluyo — y
                    # el aviso metodológico de abajo lo deja bien claro.
                    excluidas_deslistadas[meses] += 1
                    continue

                ret_bench = retorno_benchmark(fecha_entrada, meses)
                if ret_bench is None:
                    continue

                exceso = ret_empresa - ret_bench
                resultados[meses].append({
                    "ticker": ticker,
                    "retorno": ret_empresa,
                    "benchmark": ret_bench,
                    "exceso": exceso,
                })

            if i % 25 == 0:
                log.info(f"Progreso: {i}/{len(candidatos)}")
            time.sleep(PAUSA_ENTRE_TICKERS)

        # Informe final
        log.info("\n" + "=" * 60)
        log.info("RESULTADO DEL BACKTEST")
        log.info("=" * 60)

        total_candidatas = len(candidatos)

        for meses in (12, 24):
            datos = resultados[meses]
            excluidas = excluidas_deslistadas[meses]

            if not datos:
                log.info(f"\n--- Horizonte {meses} meses: sin datos suficientes ---")
                continue

            n = len(datos)
            media_empresa = sum(d["retorno"] for d in datos) / n
            media_bench = sum(d["benchmark"] for d in datos) / n
            media_exceso = sum(d["exceso"] for d in datos) / n
            ganadoras = sum(1 for d in datos if d["exceso"] > 0)
            pct_ganadoras = 100 * ganadoras / n

            # Mediana del exceso: más honesta que la media, porque una
            # sola empresa que hizo x10 puede inflar la media y dar una
            # impresión falsa de que "la estrategia funciona".
            excesos_ord = sorted(d["exceso"] for d in datos)
            mediana_exceso = excesos_ord[n // 2]

            log.info(f"\n--- Horizonte {meses} meses ---")
            log.info(f"  Empresas medidas:              {n}")
            log.info(f"  Excluidas por deslisting:      {excluidas} "
                     f"({100*excluidas/total_candidatas:.1f}% del total — SESGO OPTIMISTA)")
            log.info(f"  Retorno medio de las señales:  {media_empresa*100:+.1f}%")
            log.info(f"  Retorno medio del Russell2000: {media_bench*100:+.1f}%")
            log.info(f"  EXCESO MEDIO sobre el mercado:  {media_exceso*100:+.1f}%")
            log.info(f"  Exceso MEDIANO (más honesto):  {mediana_exceso*100:+.1f}%")
            log.info(f"  % que batió al mercado:        {pct_ganadoras:.1f}%")

        log.info("\n" + "=" * 60)
        log.info("AVISO METODOLÓGICO")
        log.info("=" * 60)
        log.info("Las empresas deslistadas se EXCLUYERON del cálculo. Eso mete")
        log.info("un sesgo optimista: las que quebraron o fueron absorbidas a mal")
        log.info("precio no restan. El exceso real de una estrategia invertible")
        log.info("sería MENOR que el que muestra este informe. El número honesto")
        log.info("es 'exceso mediano', no la media, y siempre con el % de excluidas")
        log.info("a la vista.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
