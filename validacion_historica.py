"""
validacion_historica.py — reconstruyo el market cap REAL que tenía
cada empresa en el momento exacto en que detecté un cluster de
insiders, en vez de usar el market cap de HOY.

Esto corrige el problema que vimos en descubrir_clusters_historicos.py:
esa lista mezclaba empresas que YA eran gigantes cuando ocurrió el
cluster (McDonald's, Qualcomm, Mondelez...) con empresas que de verdad
eran pequeñas entonces y crecieron después (posibles Bloom Energy,
CrowdStrike, Datadog...). Sin el market cap del momento exacto, es
imposible distinguir las dos cosas — y confundirlas invalida cualquier
conclusión sobre si la hipótesis del proyecto se sostiene.

Es un script de solo lectura. No toca ninguna tabla de producción.

Cómo reconstruyo el market cap histórico:
  1. Repito la búsqueda de clusters (reutilizo detectar_cluster_buying
     de filtro_capa1.py, no reescribo el algoritmo)
  2. Para cada cluster detectado, busco el precio de cierre REAL en la
     fecha de detección con yfinance.history() — un endpoint mucho más
     estable que .info porque no depende de scraping frágil
  3. Busco el shares_outstanding del trimestre XBRL más reciente ANTES
     de esa fecha (ya lo tengo en metricas_trimestrales, no descargo
     nada nuevo para esto — es un dato que ya recogió el enriquecedor)
  4. market_cap_historico = precio_en_esa_fecha × shares_outstanding
  5. Me quedo solo con las que en ESE momento estaban en el rango
     50M-2B — esas son las candidatas que de verdad validan la hipótesis

Cómo usarlo:
  python validacion_historica.py
  python validacion_historica.py --limite 100   # para probar rápido antes
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

# Igual que en mercado_yfinance.py: sin pausa, Yahoo empieza a bloquear
# temporalmente con miles de peticiones seguidas.
PAUSA_ENTRE_TICKERS = 0.6


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def obtener_shares_outstanding_historico(conn, empresa_id: int, fecha_corte):
    """
    Busco el shares_outstanding del trimestre XBRL más reciente cuya
    fecha_fin sea anterior o igual a fecha_corte. Es el dato más cercano
    en el tiempo que puedo usar sin mirar información del futuro — si
    tomara el trimestre siguiente, estaría haciendo trampa con datos
    que en esa fecha aún no existían.

    Descarto valores absurdamente pequeños (< 10.000 acciones). Lo
    encontré con un caso real: una empresa tenía shares_outstanding=100
    en un trimestre y 295.000.000 en el siguiente — un salto de seis
    órdenes de magnitud, imposible en la realidad. La etiqueta XBRL
    EntityCommonStockSharesOutstanding a veces reporta por clase de
    acciones en vez del total de la empresa, y ese "100" corrompía el
    cálculo de market cap con un resultado de unos pocos miles de
    dólares — para una empresa real con insiders comprando cientos de
    miles en acciones. Ninguna empresa cotizada real tiene tan pocas
    acciones en circulación, así que prefiero saltar al trimestre
    anterior válido antes que usar un dato claramente corrupto.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select shares_outstanding
            from metricas_trimestrales
            where empresa_id = %s
              and fecha_fin <= %s
              and shares_outstanding is not null
              and shares_outstanding > 10000
            order by fecha_fin desc
            limit 1
            """,
            (empresa_id, fecha_corte)
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def obtener_precio_historico(ticker: str, fecha_corte):
    """
    Descargo el precio de cierre real en la fecha del cluster usando
    history(), no .info — .info da el precio de HOY, no sirve para
    reconstruir el pasado. history() es además más estable: no depende
    del scraping frágil que rompe con frecuencia.

    Uso una ventana de +/- días porque fecha_corte puede caer en fin
    de semana o festivo bursátil, sin cotización ese día exacto. Me
    quedo con la fecha de cotización más cercana ANTERIOR O IGUAL a
    fecha_corte — nunca miro un precio posterior, porque eso sería
    exactamente el look-ahead bias que quiero evitar.

    Uso auto_adjust=False a propósito. Por defecto, yfinance devuelve
    el precio "ajustado" retroactivamente por cualquier split o
    dividendo posterior a la fecha consultada — útil para comparar en
    un gráfico, pero venenoso para mí: si una empresa hizo un split en
    2022, el precio "ajustado" de 2019 que devolvería por defecto no es
    el precio real al que cotizaba en 2019, es una cifra recalculada
    con información del futuro. Como mi shares_outstanding de XBRL sí
    es el número de acciones real que existía en ese momento (sin
    ajustar retroactivamente), necesito el precio SIN ajustar para que
    los dos números sean consistentes entre sí.
    """
    try:
        t = yf.Ticker(ticker)
        inicio = (fecha_corte - timedelta(days=10)).isoformat()
        fin = (fecha_corte + timedelta(days=3)).isoformat()
        hist = t.history(start=inicio, end=fin, auto_adjust=False)

        if hist.empty:
            return None

        hist = hist.reset_index()
        hist["Date"] = hist["Date"].dt.date
        anteriores = hist[hist["Date"] <= fecha_corte]

        if anteriores.empty:
            return None

        return float(anteriores.iloc[-1]["Close"])

    except Exception as e:
        log.warning(f"Sin precio histórico para {ticker} en {fecha_corte}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Reconstruye el market cap histórico real de los clusters detectados"
    )
    parser.add_argument(
        "--limite", type=int,
        help="Procesa solo las primeras N candidatas (para probar rápido)"
    )
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion(conn)
        dias_ventana = int(config.get("dias_ventana_cluster", 60))
        min_insiders = int(config.get("min_insiders_cluster", 3))
        cap_min = int(config.get("market_cap_min", 50_000_000))
        cap_max = int(config.get("market_cap_max", 2_000_000_000))

        log.info("Recalculando clusters históricos (misma lógica que producción)...")
        compras_por_empresa = obtener_todas_las_compras(conn)
        info_empresas = obtener_info_empresas(conn)

        candidatos = []
        for empresa_id, transacciones in compras_por_empresa.items():
            if len(transacciones) < min_insiders:
                continue
            resultado = detectar_cluster_buying(transacciones, dias_ventana, min_insiders)
            if resultado["cumple"]:
                info = info_empresas.get(empresa_id)
                if info and info[0] and resultado["fecha_deteccion"]:
                    candidatos.append((empresa_id, info[0], resultado["fecha_deteccion"]))

        if args.limite:
            candidatos = candidatos[:args.limite]

        log.info(f"Reconstruyendo market cap histórico de {len(candidatos)} candidatas...")
        log.info(f"Rango a validar: ${cap_min:,} - ${cap_max:,}")

        validadas = []
        fuera_de_rango = []
        deslistadas_o_sin_precio = []
        sin_shares_historico = []

        for i, (empresa_id, ticker, fecha_deteccion) in enumerate(candidatos, 1):
            shares = obtener_shares_outstanding_historico(conn, empresa_id, fecha_deteccion)
            precio = obtener_precio_historico(ticker, fecha_deteccion)

            # Distingo POR QUÉ no se pudo validar, no solo que no se pudo.
            # "No tengo shares" y "no tengo precio (deslistada)" son dos
            # historias completamente distintas: la primera es un hueco en
            # mis propios datos XBRL, la segunda es evidencia real de
            # supervivencia — empresas que tuvieron la señal correcta pero
            # no llegaron a sobrevivir hasta hoy para poder validarlas con
            # precio de mercado actual.
            if shares is None:
                sin_shares_historico.append(ticker)
            elif precio is None:
                deslistadas_o_sin_precio.append(ticker)
            else:
                market_cap_historico = shares * precio
                if cap_min <= market_cap_historico <= cap_max:
                    validadas.append({
                        "empresa_id": empresa_id,
                        "ticker": ticker,
                        "fecha_deteccion": fecha_deteccion,
                        "market_cap_historico": market_cap_historico,
                    })
                    log.info(
                        f"VALIDADA: {ticker} — {fecha_deteccion} — "
                        f"market cap real en su momento: ${market_cap_historico:,.0f}"
                    )
                else:
                    fuera_de_rango.append({
                        "ticker": ticker,
                        "fecha_deteccion": fecha_deteccion,
                        "market_cap_historico": market_cap_historico,
                    })

            if i % 50 == 0:
                log.info(
                    f"Progreso: {i}/{len(candidatos)} — validadas: {len(validadas)} | "
                    f"fuera de rango: {len(fuera_de_rango)} | "
                    f"deslistadas/sin precio: {len(deslistadas_o_sin_precio)} | "
                    f"sin shares históricos: {len(sin_shares_historico)}"
                )

            time.sleep(PAUSA_ENTRE_TICKERS)

        log.info(f"\n=== RESULTADO FINAL ===")
        log.info(f"Candidatas con cluster histórico (market cap de hoy, sin filtrar): {len(candidatos)}")
        log.info("")
        log.info(f"  Validadas (small/mid cap real en el momento):    {len(validadas)}")
        log.info(f"  Fuera de rango (cluster real, pero ya eran otro tamaño): {len(fuera_de_rango)}")
        log.info(f"  Deslistadas o sin precio histórico en Yahoo:     {len(deslistadas_o_sin_precio)}")
        log.info(f"  Sin dato de shares_outstanding para esa fecha:   {len(sin_shares_historico)}")

        # Guardo las validadas en CSV — sin esto, la lista solo vive en
        # el log de la terminal y se pierde al cerrarla. Necesito este
        # archivo como fuente estable para poder muestrear después el
        # corpus histórico que alimenta el RAG de la Capa 3.
        #
        # Envuelvo esto en try/except a propósito: after casi una hora
        # de cálculo real, lo último que quiero es perder el resultado
        # entero por un PermissionError tonto (Excel con el archivo
        # abierto, OneDrive bloqueándolo). Si falla el sitio normal,
        # caigo al directorio de usuario como último recurso.
        import csv
        filas_csv = [
            [v["empresa_id"], v["ticker"], v["fecha_deteccion"], v["market_cap_historico"]]
            for v in sorted(validadas, key=lambda x: x["fecha_deteccion"])
        ]

        rutas_intentar = [
            "validadas_historicas.csv",
            os.path.join(os.path.expanduser("~"), "validadas_historicas.csv"),
            os.path.join(os.path.expanduser("~"), "Desktop", "validadas_historicas.csv"),
        ]

        guardado = False
        for ruta_csv in rutas_intentar:
            try:
                with open(ruta_csv, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["empresa_id", "ticker", "fecha_deteccion", "market_cap_historico"])
                    writer.writerows(filas_csv)
                log.info(f"\nGuardado {len(validadas)} validadas en {ruta_csv}")
                guardado = True
                break
            except PermissionError as e:
                log.warning(f"No pude escribir en {ruta_csv} ({e}) — probando otra ruta")

        if not guardado:
            # Último recurso: imprimo el CSV completo por consola para
            # que, aunque no se haya podido guardar en ningún sitio,
            # el resultado de una hora de cálculo no se pierda del todo.
            log.error(
                "\nNo pude guardar el CSV en ninguna ruta. Copia manualmente "
                "estas líneas a un archivo validadas_historicas.csv:\n"
            )
            print("empresa_id,ticker,fecha_deteccion,market_cap_historico")
            for fila in filas_csv:
                print(",".join(str(x) for x in fila))

        log.info("\n--- Validadas: la evidencia real de la hipótesis ---")
        for v in sorted(validadas, key=lambda x: x["fecha_deteccion"]):
            log.info(
                f"  {v['ticker']:<8} {str(v['fecha_deteccion']):>12}  "
                f"${v['market_cap_historico']:>15,.0f}"
            )

        log.info("\n--- Deslistadas: tuvieron la señal pero no sobrevivieron para validarlas ---")
        log.info(
            "  (esto es survivorship bias real, no un fallo del script — "
            "documentarlo es parte honesta del análisis)"
        )
        for t in deslistadas_o_sin_precio:
            log.info(f"  {t}")

        if fuera_de_rango:
            log.info("\n--- Fuera de rango: tenían cluster pero ya eran otro tamaño en ese momento ---")
            for f in sorted(fuera_de_rango, key=lambda x: x["market_cap_historico"])[:15]:
                log.info(
                    f"  {f['ticker']:<8} {str(f['fecha_deteccion']):>12}  "
                    f"${f['market_cap_historico']:>15,.0f}"
                )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
