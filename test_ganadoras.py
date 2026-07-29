"""
test_ganadoras.py — el test del recall: de las empresas que SÍ
multiplicaron desde small cap dentro de mi ventana de datos, ¿cuántas
mostraron mi señal (cluster de insiders) ANTES de subir?

Por qué existe: la pregunta "¿habría cazado a Tesla/Amazon/Netflix?" no
tiene respuesta científica — su fase pre-explosiva es anterior al Form 4
electrónico (2003) y al XBRL fiable (2012), o nunca fueron small caps
cotizadas (Spotify debutó valiendo 26.000M). La pregunta contestable es
esta: entre las GANADORAS REALES de mi propia ventana de datos, ¿qué
porcentaje era detectable? Eso es el recall del sistema — el complemento
del backtest ciego de la Capa 3, que mide precisión.

Cómo funciona:
  1. Candidatas a "ganadora": empresas de mi BD que HOY capitalizan
     >= 2B (se graduaron del universo small cap) y tienen trimestres
     históricos con acciones en circulación.
  2. Para cada una, reconstruyo su market cap DE LA ÉPOCA (precio
     histórico de yfinance x acciones del trimestre, la misma técnica
     que validacion_historica.py) y busco si alguna vez estuvo en el
     rango 50M-2B dentro de la ventana de datos.
  3. Ganadora = multiplicó por --multiplo (3x por defecto) desde aquel
     mínimo small cap hasta hoy.
  4. Para cada ganadora, compruebo si hubo un cluster real (3+ insiders
     comprando en 60 días — exactamente la señal de producción, reutilizo
     detectar_cluster_buying) en fecha en que AÚN era small cap.
  5. Recall = ganadoras con señal / ganadoras totales.

Sesgos que declaro:
  - Solo veo ganadoras que siguen vivas y en mi BD (las adquiridas a
    mitad de subida no aparecen) — el recall real sería algo distinto.
  - El techo de 20B por defecto (--cap-max) existe para no perder tiempo
    reconstruyendo megacaps que nunca fueron small caps en la ventana.

Es un script de solo lectura. No toca ninguna tabla.

Cómo usarlo:
  python test_ganadoras.py --limite 50      # prueba rápida
  python test_ganadoras.py                  # completo (~30-60 min, yfinance)
"""

import os
import csv
import time
import logging
import argparse
from datetime import timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()

from filtro_capa1 import detectar_cluster_buying
from validacion_historica import obtener_precio_historico

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# yfinance grita un ERROR por cada trimestre sin datos (empresas que
# salieron a bolsa a mitad de la ventana, deslistadas, etc.). No son
# errores nuestros — el script ya salta esos trimestres — así que lo
# silencio para que no entierre las líneas que sí importan.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

ARCHIVO_RESULTADOS = "resultados_test_ganadoras.csv"
PAUSA_ENTRE_TICKERS = 0.6

SMALLCAP_MIN = 50_000_000
SMALLCAP_MAX = 2_000_000_000


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def candidatas_a_ganadora(conn, cap_max: int, limite):
    """
    Empresas que hoy están POR ENCIMA del universo small cap y tienen
    histórico de trimestres con acciones en circulación — las únicas
    cuyas caps de época puedo reconstruir.
    """
    cur = conn.cursor()
    try:
        query = """
            select e.id, e.ticker, e.nombre, e.market_cap_usd
            from empresas e
            where e.market_cap_usd >= %s
              and e.market_cap_usd <= %s
              and exists (select 1 from metricas_trimestrales mt
                          where mt.empresa_id = e.id
                            and mt.shares_outstanding is not null)
            order by e.market_cap_usd
        """
        if limite:
            query += f" limit {int(limite)}"
        cur.execute(query, (SMALLCAP_MAX, cap_max))
        return cur.fetchall()
    finally:
        cur.close()


def trimestres_con_acciones(conn, empresa_id: int) -> list:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select fecha_fin, shares_outstanding
            from metricas_trimestrales
            where empresa_id = %s and shares_outstanding is not null
              and fecha_fin is not null
            order by fecha_fin
            """,
            (empresa_id,)
        )
        return cur.fetchall()
    finally:
        cur.close()


def compras_p(conn, empresa_id: int) -> list:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select fecha_transaccion, nombre_insider
            from insider_transactions
            where empresa_id = %s and tipo_transaccion = 'P'
              and fecha_transaccion is not null
            order by fecha_transaccion
            """,
            (empresa_id,)
        )
        return cur.fetchall()
    finally:
        cur.close()


def minimo_smallcap_historico(conn, ticker: str, empresa_id: int):
    """
    El momento más barato en que la empresa fue small cap dentro de la
    ventana. Muestreo los trimestres (no hace falta el mínimo exacto al
    céntimo — hace falta saber si ESTUVO en el rango y desde qué base
    multiplicó). Devuelvo (fecha, cap) o None.
    """
    mejor = None
    for fecha_fin, shares in trimestres_con_acciones(conn, empresa_id):
        precio = obtener_precio_historico(ticker, fecha_fin)
        time.sleep(PAUSA_ENTRE_TICKERS)
        if precio is None or not shares:
            continue
        cap = float(precio) * float(shares)
        if SMALLCAP_MIN <= cap <= SMALLCAP_MAX:
            if mejor is None or cap < mejor[1]:
                mejor = (fecha_fin, cap)
    return mejor


def metricas_precio(ticker: str, fecha_min, fecha_cluster):
    """
    Con una sola descarga de la serie ajustada saco tres cosas:
      - multiplo_pico: del primer cierre tras el mínimo small cap al
        MÁXIMO posterior. Es el techo teórico, no lo que ganarías —
        nadie vende en el pico — pero como referencia no miente.
      - multiplo_pico_desde_senal: lo mismo pero desde la fecha del
        cluster, si lo hubo. Lo que la señal "ofrecía" como máximo.
      - posicion_52w_cluster: dónde estaba el precio dentro de su rango
        de 52 semanas EL DÍA del cluster (0=mínimos, 1=máximos). Este
        dato decide con evidencia si el score de "cerca de mínimos" de
        la Capa 2 refleja el patrón real de las ganadoras o hay que
        bajarle el peso.
    """
    import yfinance as yf
    from datetime import timedelta as _td

    inicio = min([f for f in (fecha_min, fecha_cluster) if f]) - _td(days=380)
    try:
        datos = yf.download(ticker, start=inicio.isoformat(),
                            auto_adjust=True, progress=False)
        if datos is None or datos.empty:
            return {}
        cierres = datos["Close"]
        if hasattr(cierres, "columns"):
            cierres = cierres.iloc[:, 0]
        cierres = cierres.dropna()
    except Exception:
        return {}

    def _en(fecha):
        tramo = cierres[cierres.index.date >= fecha]
        return float(tramo.iloc[0]) if len(tramo) else None

    resultado = {}

    base_min = _en(fecha_min)
    if base_min:
        posteriores = cierres[cierres.index.date >= fecha_min]
        if len(posteriores):
            resultado["multiplo_pico"] = round(float(posteriores.max()) / base_min, 1)

    if fecha_cluster:
        base_cl = _en(fecha_cluster)
        if base_cl:
            posteriores = cierres[cierres.index.date >= fecha_cluster]
            if len(posteriores):
                resultado["multiplo_pico_desde_senal"] = round(
                    float(posteriores.max()) / base_cl, 1)

            ventana_52w = cierres[
                (cierres.index.date >= fecha_cluster - _td(days=365))
                & (cierres.index.date <= fecha_cluster)
            ]
            if len(ventana_52w) >= 60:
                minimo, maximo = float(ventana_52w.min()), float(ventana_52w.max())
                if maximo > minimo:
                    resultado["posicion_52w_cluster"] = round(
                        (base_cl - minimo) / (maximo - minimo), 2)

    return resultado


def hubo_cluster_siendo_small(compras: list, fecha_limite) -> tuple:
    """
    ¿Hubo cluster (3+ insiders / 60 días) con fecha de detección
    anterior o igual al periodo small cap? Reutilizo la señal EXACTA de
    producción — no una versión suavizada para que el test salga bonito.
    """
    hasta_limite = [
        (fecha, nombre) for fecha, nombre in compras
        if fecha <= fecha_limite + timedelta(days=365)
    ]
    if len(hasta_limite) < 3:
        return False, None
    resultado = detectar_cluster_buying(hasta_limite, 60, 3)
    if resultado.get("cumple"):
        return True, resultado.get("fecha_deteccion")
    return False, None


def main():
    parser = argparse.ArgumentParser(
        description="Recall del sistema: ¿las ganadoras reales eran detectables?"
    )
    parser.add_argument("--limite", type=int, default=None,
                        help="Máximo de empresas graduadas a revisar")
    parser.add_argument("--multiplo", type=float, default=3.0,
                        help="Múltiplo mínimo para considerar 'ganadora' (por defecto 3x)")
    parser.add_argument("--cap-max", type=int, default=20_000_000_000,
                        help="Techo de cap actual (evita megacaps nunca small en ventana)")
    args = parser.parse_args()

    conn = conectar_db()
    ganadoras, con_senal = [], 0
    revisadas, nunca_small = 0, 0

    try:
        candidatas = candidatas_a_ganadora(conn, args.cap_max, args.limite)
        log.info(f"Empresas graduadas (cap actual 2B-{args.cap_max/1e9:.0f}B): {len(candidatas)}")

        for empresa_id, ticker, nombre, cap_hoy in candidatas:
            revisadas += 1
            minimo = minimo_smallcap_historico(conn, ticker, empresa_id)
            if minimo is None:
                nunca_small += 1
                continue

            fecha_min, cap_min = minimo
            multiplo = float(cap_hoy) / cap_min
            if multiplo < args.multiplo:
                continue

            compras = compras_p(conn, empresa_id)
            senal, fecha_cluster = hubo_cluster_siendo_small(compras, fecha_min)
            if senal:
                con_senal += 1

            extras = metricas_precio(ticker, fecha_min, fecha_cluster)
            time.sleep(PAUSA_ENTRE_TICKERS)

            ganadoras.append({
                "ticker": ticker,
                "nombre": nombre,
                "fecha_min_smallcap": fecha_min.isoformat(),
                "cap_minima": round(cap_min),
                "cap_actual": int(cap_hoy),
                "multiplo": round(multiplo, 1),
                "multiplo_pico": extras.get("multiplo_pico", ""),
                "tuvo_cluster": senal,
                "fecha_cluster": fecha_cluster.isoformat() if fecha_cluster else "",
                "multiplo_pico_desde_senal": extras.get("multiplo_pico_desde_senal", ""),
                "posicion_52w_cluster": extras.get("posicion_52w_cluster", ""),
            })
            pos = extras.get("posicion_52w_cluster")
            log.info(
                f"{ticker}: x{multiplo:.1f} (pico x{extras.get('multiplo_pico', '?')}) "
                f"desde {cap_min/1e6:.0f}M ({fecha_min}) — "
                f"{'CON señal' if senal else 'sin señal'}"
                + (f", cluster en pos 52w {pos:.2f}" if pos is not None else "")
            )

            if revisadas % 25 == 0:
                log.info(f"Progreso: {revisadas}/{len(candidatas)} revisadas, "
                         f"{len(ganadoras)} ganadoras, {con_senal} con señal")
    finally:
        conn.close()

    if ganadoras:
        with open(ARCHIVO_RESULTADOS, "w", newline="", encoding="utf-8") as f:
            escritor = csv.DictWriter(f, fieldnames=list(ganadoras[0].keys()))
            escritor.writeheader()
            escritor.writerows(ganadoras)

    print("\n" + "=" * 60)
    print(f"TEST DE RECALL — ¿las ganadoras reales eran detectables?")
    print("=" * 60)
    print(f"Empresas graduadas revisadas:        {revisadas}")
    print(f"  (nunca fueron small cap en ventana: {nunca_small})")
    print(f"Ganadoras (>= x{args.multiplo:.0f} desde small cap):  {len(ganadoras)}")
    print(f"Ganadoras CON cluster siendo small:  {con_senal}")
    if ganadoras:
        print(f"RECALL de la señal de Capa 1:        {con_senal/len(ganadoras):.0%}")

        from statistics import median as _mediana
        picos = [g["multiplo_pico"] for g in ganadoras if g["multiplo_pico"] != ""]
        if picos:
            print(f"Mediana del multiplo PICO (techo teorico): x{_mediana(picos):.1f}")
        picos_senal = [g["multiplo_pico_desde_senal"] for g in ganadoras
                       if g["multiplo_pico_desde_senal"] != ""]
        if picos_senal:
            print(f"Mediana del pico DESDE la senal:     x{_mediana(picos_senal):.1f}")
        posiciones = [g["posicion_52w_cluster"] for g in ganadoras
                      if g["posicion_52w_cluster"] != ""]
        if posiciones:
            print(f"Posicion 52w mediana en el cluster:  {_mediana(posiciones):.2f}"
                  f"  (0=minimos, 1=maximos; n={len(posiciones)})")
            print("  -> si sale >0.4, el peso de 'cerca de minimos' de la")
            print("     Capa 2 merece revision: las ganadoras no esperan al suelo.")
    print("=" * 60)
    print(f"Detalle en {ARCHIVO_RESULTADOS}")
    print("Sesgo declarado: solo ganadoras vivas y en la BD — las")
    print("adquiridas a mitad de subida no cuentan.")


if __name__ == "__main__":
    main()
