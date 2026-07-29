"""
enriquecedor_xbrl.py — descarga métricas financieras trimestrales de la SEC

Para cada empresa que ya tengo en la tabla empresas, llama a la API
de companyfacts de la SEC y extrae los datos XBRL de los últimos N trimestres.

La SEC tiene una API JSON gratuita que devuelve todos los datos históricos
de una empresa en una sola llamada. No hace falta parsear XML.

URL del endpoint:
  https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

Cómo usarlo:
  # Enriquece todas las empresas activas (modo producción):
  python enriquecedor_xbrl.py

  # Solo un CIK concreto (para depurar o testear):
  python enriquecedor_xbrl.py --cik 0000320193

  # Limita cuántas empresas procesa (útil para probar sin esperar horas):
  python enriquecedor_xbrl.py --limite 50

  # Reprocesa las empresas que YA tienen trimestres guardados — lo uso
  # después de un fix en el extractor (como el fallback de gross_profit)
  # para aplicar la lógica nueva sin rehacer el backfill desde cero.
  # on conflict do update hace que sea seguro ejecutarlo las veces que
  # haga falta. (Antes esto era un script aparte, reprocesar_xbrl.py.)
  python enriquecedor_xbrl.py --reprocesar
"""

import os
import time
import logging
import argparse
from datetime import datetime, date, timedelta
from typing import Optional

import requests
import psycopg2
from psycopg2.extras import execute_values

from normalizar import normalizar_cik
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

# La SEC obligó a las "smaller reporting companies" (las small caps que
# analiza este sistema) a presentar XBRL a partir del primer 10-Q con
# periodo fiscal cerrado el 15 de junio de 2011 o después. Los trimestres
# justo posteriores al mandato (2011-2012) además tienen peor calidad —
# un estudio académico encontró un declive medible en la comparabilidad
# de los datos durante esa fase de transición, porque las empresas estaban
# aprendiendo a etiquetar. Por eso pongo el corte un año después de la
# fecha legal, no justo en el límite.
FECHA_CORTE_XBRL_FIABLE = date(2012, 6, 15)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# La SEC exige un User-Agent con contacto REAL — email falso = riesgo de 403.
SEC_HEADERS = {
    "User-Agent": f"SmallCapHunter {os.getenv('SEC_CONTACT_EMAIL', 'configura-SEC_CONTACT_EMAIL@en-tu-.env')}",
    "Accept-Encoding": "gzip, deflate",
}

# La SEC exige máximo 10 req/seg. Me quedo en 8 para tener margen.
# Con miles de empresas el backfill tarda horas — es lo esperado.
PAUSA_ENTRE_REQUESTS = 0.13


# Mapa de etiquetas XBRL a los campos de mi tabla.
# La API de la SEC devuelve los datos bajo el namespace us-gaap.
# Cada etiqueta puede tener múltiples variantes históricas — las fusiono
# todas en una sola serie (ver extraer_serie_xbrl), con las primeras de
# cada lista ganando cuando hay conflicto de fechas.
XBRL_CAMPOS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "gross_profit": [
        "GrossProfit",
    ],
    # No es un campo de mi tabla — lo uso solo para derivar gross_profit
    # cuando la empresa no reporta GrossProfit directamente. Muchos
    # retailers y marketplaces (Amazon, Walmart) no usan la etiqueta
    # GrossProfit; reportan el coste de ventas y hay que restar.
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
    ],
    "net_income": [
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "accounts_receivable": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
    ],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ],
}


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def descargar_companyfacts(cik: str) -> Optional[dict]:
    """
    Descarga el JSON de companyfacts de la SEC para un CIK dado.
    Este endpoint devuelve TODOS los datos XBRL históricos de la empresa
    en una sola llamada — es la forma más eficiente de acceder a los datos.

    Reintento hasta 3 veces con backoff exponencial si hay errores de red.
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    for intento in range(3):
        try:
            r = requests.get(url, headers=SEC_HEADERS, timeout=20)

            if r.status_code == 404:
                # La empresa existe en mi BD pero no tiene datos XBRL.
                # Pasa con empresas muy pequeñas o que dejaron de cotizar
                # antes de que la SEC implantara XBRL (anterior a 2009).
                return None

            if r.status_code == 429:
                espera = 60 * (intento + 1)
                log.warning(f"Rate limit SEC — espero {espera}s")
                time.sleep(espera)
                continue

            r.raise_for_status()
            return r.json()

        except requests.exceptions.Timeout:
            log.warning(f"Timeout para CIK {cik} — intento {intento+1}/3")
            time.sleep(5 * (intento + 1))
        except requests.exceptions.JSONDecodeError:
            log.error(f"Respuesta no válida para CIK {cik}")
            return None
        except Exception as e:
            log.error(f"Error descargando CIK {cik}: {e}")
            if intento == 2:
                return None
            time.sleep(3)

    return None


def descargar_sic(cik: str) -> Optional[dict]:
    """
    Descarga el SIC (código de sector) y algunos metadatos básicos
    desde el endpoint de submissions de la SEC. Es una llamada aparte
    de companyfacts porque la SEC los separa en dos endpoints distintos.

    Uso esto para poder tratar cada sector con sus métricas propias:
    un banco no tiene "gross profit" en el sentido tradicional, un REIT
    tampoco — sin el SIC no puedo distinguir "dato ausente por error"
    de "dato que no aplica a este tipo de negocio".
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"

    for intento in range(3):
        try:
            r = requests.get(url, headers=SEC_HEADERS, timeout=20)

            if r.status_code == 404:
                return None

            if r.status_code == 429:
                espera = 60 * (intento + 1)
                log.warning(f"Rate limit SEC (submissions) — espero {espera}s")
                time.sleep(espera)
                continue

            r.raise_for_status()
            data = r.json()
            return {
                "sic":             data.get("sic"),
                "sic_description": data.get("sicDescription"),
                "nombre":          data.get("name"),
            }

        except requests.exceptions.Timeout:
            log.warning(f"Timeout SIC para CIK {cik} — intento {intento+1}/3")
            time.sleep(5 * (intento + 1))
        except requests.exceptions.JSONDecodeError:
            return None
        except Exception as e:
            log.error(f"Error descargando SIC de CIK {cik}: {e}")
            if intento == 2:
                return None
            time.sleep(3)

    return None


def _duracion_dias(entrada: dict) -> Optional[int]:
    """
    Calcula la duración en días de un periodo XBRL usando start y end.
    Lo necesito para distinguir datos trimestrales (~90 días) de los
    acumulados (180, 270) y anuales (365).

    Los valores de balance (shares, accounts receivable) no tienen start
    porque son una foto puntual, no un periodo. En ese caso devuelvo None
    y los trato aparte.
    """
    inicio = entrada.get("start")
    fin = entrada.get("end")
    if not inicio or not fin:
        return None
    try:
        d_inicio = datetime.strptime(inicio, "%Y-%m-%d").date()
        d_fin = datetime.strptime(fin, "%Y-%m-%d").date()
        return (d_fin - d_inicio).days
    except (ValueError, TypeError):
        return None


def _es_trimestral(entrada: dict) -> bool:
    """
    Un periodo es trimestral si dura entre 80 y 100 días.
    Uso un rango en lugar de exactamente 90 porque los trimestres reales
    varían: febrero acorta, algunos cierres caen en fin de semana, etc.
    """
    dias = _duracion_dias(entrada)
    return dias is not None and 80 <= dias <= 100


def extraer_serie_xbrl(facts: dict, etiquetas: list, es_flujo: bool) -> dict:
    """
    Dentro del JSON de companyfacts, los datos están en:
      facts -> us-gaap -> {etiqueta} -> units -> USD -> [{...}]

    Cada elemento tiene: start, end, val, form, fp, fy, frame (opcional).

    Aquí está la parte delicada. Hay dos tipos de métrica:

    - Métricas de FLUJO (revenue, net income, cash flow): la SEC las reporta
      de forma ACUMULADA dentro del año fiscal. El Q2 trae 6 meses, no 3.
      Solo me quedo con los periodos de ~90 días para tener el trimestre real.

    - Métricas de STOCK (shares outstanding, accounts receivable): son una
      foto puntual en la fecha de cierre, no un periodo. No las filtro por
      duración porque no tienen 'start'.

    FUSIÓN DE ETIQUETAS. La primera versión devolvía la primera etiqueta
    que tuviera datos y descartaba las demás. Eso truncaba series enteras:
    muchas empresas cambiaron de SalesRevenueNet a
    RevenueFromContractWithCustomer... en 2018 (la transición a ASC 606),
    y quedarme con una sola etiqueta significaba perder la mitad de la
    serie justo donde el análisis de aceleración necesita continuidad.
    Ahora recorro TODAS las etiquetas en orden de prioridad y fusiono:
    la primera etiqueta que aporta una fecha "gana" esa fecha, y las
    siguientes solo rellenan los huecos que queden.

    Aviso sobre el Q4: la SEC no publica el Q4 por separado en los 10-Q,
    solo el año completo en el 10-K. Eso significa que para el cuarto
    trimestre me faltará el dato trimestral de flujo salvo que lo derive
    restando (FY - Q1 - Q2 - Q3). De momento lo dejo fuera; si el análisis
    de un sector concreto lo necesita, añado esa resta como paso posterior.

    Cuando hay reexpresiones (la SEC republica el mismo periodo tras una
    enmienda), me quedo con la que tiene el 'fy'/'fp' más reciente, que es
    la versión corregida más fiable.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    orden_fp = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}

    serie = {}

    for etiqueta in etiquetas:
        if etiqueta not in us_gaap:
            continue

        units = us_gaap[etiqueta].get("units", {})
        datos = units.get("USD") or units.get("shares") or []
        if not datos:
            continue

        # Solo filings periódicos reales
        datos = [d for d in datos if d.get("form") in ("10-Q", "10-K")]

        if es_flujo:
            # Para métricas de flujo, quedarme solo con los trimestres reales
            # (~90 días) resuelve el problema del acumulado de golpe.
            datos = [d for d in datos if _es_trimestral(d)]
        # Para métricas de stock no filtro por duración: no tienen periodo.

        if not datos:
            continue

        # Dentro de esta etiqueta, deduplico por fecha de fin quedándome
        # con la reexpresión más reciente: ordeno por (fy, fp) ascendente
        # y la última gana.
        parcial = {}
        for d in sorted(
            datos,
            key=lambda x: (x.get("fy") or 0, orden_fp.get(x.get("fp"), 0))
        ):
            parcial[d["end"]] = d

        # Fusiono con lo acumulado: las etiquetas anteriores (más
        # prioritarias) conservan sus fechas; esta solo rellena huecos.
        for fecha_fin, d in parcial.items():
            if fecha_fin not in serie:
                serie[fecha_fin] = d

    return serie


def fecha_a_trimestre(fecha_str: str) -> tuple:
    """
    Convierte una fecha de fin de trimestre al año y trimestre fiscal.
    La SEC usa la fecha de fin del periodo — ej: 2023-03-31 es Q1 para Apple
    pero podría ser Q2 para una empresa con año fiscal diferente.

    Uso el mes de fin de periodo para inferir el trimestre:
      Mes 3  (marzo)     -> Q1 de muchas empresas con año fiscal enero-diciembre
      Mes 6  (junio)     -> Q2
      Mes 9  (septiembre)-> Q3
      Mes 12 (diciembre) -> Q4

    Esto es una aproximación — el año fiscal real está en el campo 'fy'
    del JSON de la SEC, que uso cuando está disponible.
    """
    try:
        d = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        mes = d.month

        # Mapeo mes de cierre a trimestre fiscal aproximado
        if mes in (1, 2, 3):
            return d.year, 1
        elif mes in (4, 5, 6):
            return d.year, 2
        elif mes in (7, 8, 9):
            return d.year, 3
        else:
            return d.year, 4

    except (ValueError, TypeError):
        return None, None


def construir_trimestres(facts: dict, ventana: int) -> list:
    """
    Construye una lista de dicts con las métricas por trimestre.
    Solo devuelvo los últimos N trimestres (ventana).

    La lógica:
    1. Extraigo la serie de cada métrica XBRL (etiquetas ya fusionadas)
    2. Las fechas de fin de trimestre del revenue son la referencia
    3. Para cada fecha, construyo una fila con todos los valores
    """
    # Marco qué campos son de flujo (acumulados en el año) y cuáles de stock
    # (foto puntual). El tratamiento en extraer_serie_xbrl es distinto.
    campos_flujo = {"revenue", "gross_profit", "net_income",
                    "operating_cash_flow", "cost_of_revenue"}

    series = {}
    for campo, etiquetas in XBRL_CAMPOS.items():
        series[campo] = extraer_serie_xbrl(
            facts, etiquetas, es_flujo=(campo in campos_flujo)
        )

    # Las fechas de fin de periodo del revenue son la referencia.
    # Si no hay revenue trimestral, la empresa no me sirve para el análisis.
    if not series.get("revenue"):
        return []

    fechas = sorted(series["revenue"].keys(), reverse=True)[:ventana]

    trimestres = []
    for fecha_fin in fechas:
        entrada_rev = series["revenue"].get(fecha_fin, {})

        # Uso el año y trimestre fiscal REALES que da la SEC en los campos
        # 'fy' y 'fp'. Solo caigo en la inferencia por mes si faltan, que
        # es raro en filings modernos.
        anio = entrada_rev.get("fy")
        fp = entrada_rev.get("fp")  # "Q1", "Q2", "Q3"
        trimestre = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}.get(fp)

        if not anio or not trimestre:
            anio, trimestre = fecha_a_trimestre(fecha_fin)
        if not anio or not trimestre:
            continue

        try:
            fecha_fin_dt = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
            fecha_inicio_str = entrada_rev.get("start")
            if fecha_inicio_str:
                fecha_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
            else:
                fecha_inicio = fecha_fin_dt - timedelta(days=90)
        except (ValueError, TypeError):
            fecha_inicio = None
            fecha_fin_dt = None

        revenue_val = _valor(series["revenue"], fecha_fin)
        gross_profit_val = _valor(series["gross_profit"], fecha_fin)

        # Si la empresa no reporta GrossProfit directamente, lo derivo
        # restando el coste de ventas al revenue — es lo que haría
        # cualquier analista a mano. Solo lo calculo si tengo ambos
        # datos del MISMO trimestre; si falta cualquiera, dejo el gross
        # profit en None en vez de inventar un número a medias.
        if gross_profit_val is None:
            costo_val = _valor(series["cost_of_revenue"], fecha_fin)
            if revenue_val is not None and costo_val is not None:
                gross_profit_val = revenue_val - costo_val

        fila = {
            "anio_fiscal":          anio,
            "trimestre":            trimestre,
            "fecha_inicio":         fecha_inicio,
            "fecha_fin":            fecha_fin_dt,
            "revenue":              revenue_val,
            "gross_profit":         gross_profit_val,
            "net_income":           _valor(series["net_income"], fecha_fin),
            "operating_cash_flow":  _valor(series["operating_cash_flow"], fecha_fin),
            "accounts_receivable":  _valor(series["accounts_receivable"], fecha_fin),
            "shares_outstanding":   _valor(series["shares_outstanding"], fecha_fin),
        }

        # Descarto trimestres anteriores al corte de fiabilidad XBRL.
        # No es un límite arbitrario: antes de esa fecha las small caps
        # no estaban obligadas a presentar XBRL, o llevaban muy poco
        # tiempo haciéndolo y los datos son más propensos a errores.
        if fecha_fin_dt and fecha_fin_dt < FECHA_CORTE_XBRL_FIABLE:
            continue

        trimestres.append(fila)

    return trimestres


def _valor(serie: dict, fecha: str) -> Optional[int]:
    """Extrae el valor de una fecha dada, o None si no existe."""
    entrada = serie.get(fecha)
    if entrada is None:
        return None
    val = entrada.get("val")
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def tiene_trimestre_reciente(trimestres: list, meses_max: int = 9) -> bool:
    """
    Compruebo si el trimestre más reciente es de hace menos de N meses.
    Si no lo es, la empresa probablemente ha desaparecido o dejado de
    presentar informes y la marco como inactiva (marcar_sin_datos).
    """
    if not trimestres:
        return False

    fecha_mas_reciente = max(
        t["fecha_fin"] for t in trimestres if t.get("fecha_fin")
    )
    if not fecha_mas_reciente:
        return False

    hoy = date.today()
    diferencia_meses = (
        (hoy.year - fecha_mas_reciente.year) * 12 +
        (hoy.month - fecha_mas_reciente.month)
    )
    return diferencia_meses <= meses_max


def guardar_trimestres(conn, empresa_id: int, trimestres: list) -> int:
    """
    Inserta los trimestres en metricas_trimestrales.
    on conflict do update para actualizar si la SEC corrige datos históricos
    (pasa cuando una empresa presenta una enmienda al 10-Q).

    Antes de insertar deduplico por (anio_fiscal, trimestre). A veces la
    SEC da dos entradas para el mismo periodo fiscal — por ejemplo si la
    empresa cambió de año fiscal a mitad de camino, o hay una reexpresión
    que mi extractor no fusionó del todo. PostgreSQL no permite que un
    mismo insert con on conflict do update toque la misma fila dos veces,
    así que si no deduplico aquí, la sentencia entera falla y pierdo el
    lote completo en vez de solo el trimestre problemático.
    """
    if not trimestres:
        return 0

    # Deduplico quedándome con la última aparición de cada (anio, trimestre).
    # La lista viene ordenada de más reciente a más antigua (ver
    # construir_trimestres), así que la PRIMERA aparición es la más fiable —
    # invierto el criterio y me quedo con esa.
    vistos = {}
    for t in trimestres:
        clave = (t["anio_fiscal"], t["trimestre"])
        if clave not in vistos:
            vistos[clave] = t
    trimestres_dedup = list(vistos.values())

    duplicados_descartados = len(trimestres) - len(trimestres_dedup)
    if duplicados_descartados > 0:
        log.warning(
            f"Empresa {empresa_id}: descartados {duplicados_descartados} "
            f"trimestres duplicados (mismo año/trimestre fiscal)"
        )

    cur = conn.cursor()
    try:
        filas = [
            (
                empresa_id,
                t["anio_fiscal"],
                t["trimestre"],
                t.get("fecha_inicio"),
                t.get("fecha_fin"),
                t.get("revenue"),
                t.get("gross_profit"),
                t.get("net_income"),
                t.get("operating_cash_flow"),
                t.get("accounts_receivable"),
                t.get("shares_outstanding"),
            )
            for t in trimestres_dedup
        ]

        execute_values(
            cur,
            """
            insert into metricas_trimestrales (
                empresa_id, anio_fiscal, trimestre,
                fecha_inicio, fecha_fin,
                revenue, gross_profit, net_income,
                operating_cash_flow, accounts_receivable,
                shares_outstanding
            )
            values %s
            on conflict (empresa_id, anio_fiscal, trimestre)
            do update set
                fecha_inicio        = EXCLUDED.fecha_inicio,
                fecha_fin           = EXCLUDED.fecha_fin,
                revenue             = EXCLUDED.revenue,
                gross_profit        = EXCLUDED.gross_profit,
                net_income          = EXCLUDED.net_income,
                operating_cash_flow = EXCLUDED.operating_cash_flow,
                accounts_receivable = EXCLUDED.accounts_receivable,
                shares_outstanding  = EXCLUDED.shares_outstanding
            """,
            filas,
        )

        conn.commit()
        return len(filas)

    except Exception as e:
        conn.rollback()
        log.error(f"Error guardando trimestres para empresa {empresa_id}: {e}")
        raise
    finally:
        cur.close()


def marcar_sin_datos(conn, empresa_id: int, motivo: str):
    """
    Si la empresa no tiene datos XBRL o no tiene trimestre reciente,
    la marco como inactiva con el motivo exacto. Este es EL punto de
    baja del sistema (el trigger del schema original se eliminó por
    redundante y por marcar bajas falsas durante backfills desordenados).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            update empresas set
                activa      = false,
                estado      = 'descartada',
                fecha_baja  = now(),
                motivo_baja = %s
            where id = %s and activa = true
            """,
            (motivo, empresa_id)
        )
        conn.commit()
    finally:
        cur.close()


def guardar_sic(conn, empresa_id: int, sic: str, sic_desc: str):
    """
    Guarda el SIC en la tabla empresas. Solo actualizo si el campo
    está vacío — no quiero pisar un SIC que ya tuviera puesto a mano.
    """
    if not sic:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            update empresas set sic = %s, sector = %s
            where id = %s and (sic is null or sic = '')
            """,
            (str(sic), sic_desc, empresa_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.warning(f"No pude guardar SIC para empresa {empresa_id}: {e}")
    finally:
        cur.close()


def empresa_tiene_sic(conn, empresa_id: int) -> bool:
    """Compruebo si ya tengo el SIC antes de gastar otra llamada a la SEC."""
    cur = conn.cursor()
    try:
        cur.execute("select sic from empresas where id = %s", (empresa_id,))
        row = cur.fetchone()
        return row is not None and row[0] not in (None, "")
    finally:
        cur.close()


def enriquecer_empresa(conn, empresa_id: int, cik: str, ventana: int) -> str:
    """
    Proceso completo para una empresa:
    1. Descargo el SIC (código de sector) si no lo tengo aún
    2. Descargo el companyfacts de la SEC
    3. Extraigo los trimestres
    4. Compruebo que el último trimestre es reciente
    5. Si no lo es, la marco como inactiva
    6. Si sí, guardo los trimestres

    Devuelvo un string con el resultado para el log.
    """
    if not empresa_tiene_sic(conn, empresa_id):
        sic_data = descargar_sic(cik)
        if sic_data:
            guardar_sic(conn, empresa_id, sic_data.get("sic"), sic_data.get("sic_description"))
        # Pausa extra porque esto es una llamada HTTP adicional a la SEC
        time.sleep(PAUSA_ENTRE_REQUESTS)

    facts = descargar_companyfacts(cik)

    if facts is None:
        marcar_sin_datos(conn, empresa_id, "Sin datos XBRL en la SEC")
        return "sin_xbrl"

    trimestres = construir_trimestres(facts, ventana)

    if not trimestres:
        marcar_sin_datos(conn, empresa_id, "XBRL existe pero sin datos trimestrales útiles")
        return "sin_trimestres"

    if not tiene_trimestre_reciente(trimestres):
        # Esta empresa no ha presentado informes recientes.
        # Guardo igual los trimestres históricos porque me son útiles
        # para el backtest, pero la marco como inactiva para que el
        # pipeline de producción la ignore.
        guardar_trimestres(conn, empresa_id, trimestres)
        marcar_sin_datos(
            conn, empresa_id,
            f"Último trimestre: {max(t['fecha_fin'] for t in trimestres if t.get('fecha_fin'))} "
            f"— sin datos recientes, posible empresa desaparecida"
        )
        return "inactiva"

    n = guardar_trimestres(conn, empresa_id, trimestres)
    return f"ok:{n}"


def obtener_empresas_pendientes(conn, limite: Optional[int] = None,
                                reprocesar: bool = False) -> list:
    """
    En modo normal: empresas activas que aún no tienen métricas o cuyo
    último trimestre tiene más de 90 días — necesitan actualización.

    En modo --reprocesar: todas las empresas activas que YA tienen
    trimestres guardados, para aplicarles la lógica nueva del extractor.

    Consulto la tabla empresas directamente, no la vista empresas_activas:
    las vistas con select * congelan sus columnas al crearse y ya me
    dieron un susto con la columna 'bolsa'. Además uso solo activa=true
    (sin filtrar por estado): una empresa descartada por la Capa 1 sigue
    mereciendo datos frescos — si sus números cambian, la Capa 1 la
    reevaluará con datos de verdad, no con un snapshot congelado.
    """
    cur = conn.cursor()
    try:
        if reprocesar:
            query = """
                select distinct e.id, e.cik, e.ticker, e.nombre
                from empresas e
                join metricas_trimestrales m on m.empresa_id = e.id
                where e.activa = true
                order by e.id
            """
        else:
            query = """
                select e.id, e.cik, e.ticker, e.nombre
                from empresas e
                where e.activa = true
                and not exists (
                    select 1 from metricas_trimestrales mt
                    where mt.empresa_id = e.id
                    and mt.fecha_fin > now() - interval '90 days'
                )
                order by e.primera_deteccion desc
            """
        if limite:
            query += f" limit {limite}"

        cur.execute(query)
        return cur.fetchall()
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(
        description="Enriquece empresas con métricas XBRL de la SEC"
    )
    parser.add_argument(
        "--cik",
        type=str,
        help="Procesa solo este CIK (para depurar)"
    )
    parser.add_argument(
        "--limite",
        type=int,
        help="Máximo de empresas a procesar en esta ejecución"
    )
    parser.add_argument(
        "--ventana",
        type=int,
        default=12,
        help="Trimestres históricos a descargar por empresa (default: 12)"
    )
    parser.add_argument(
        "--reprocesar",
        action="store_true",
        help="Reprocesa las empresas que ya tienen trimestres guardados "
             "(para aplicar fixes del extractor sin rehacer el backfill)"
    )
    args = parser.parse_args()

    conn = conectar_db()

    try:
        if args.cik:
            # Modo debug: proceso solo una empresa.
            # Normalizo el CIK con la misma regla que el loader para
            # asegurar que casa con lo que hay en la base de datos.
            cik = normalizar_cik(args.cik)
            if not cik:
                log.error(f"CIK inválido: {args.cik}")
                return
            cur = conn.cursor()
            cur.execute("select id, cik, ticker, nombre from empresas where cik = %s", (cik,))
            empresa = cur.fetchone()
            cur.close()

            if not empresa:
                log.error(f"CIK {cik} no encontrado en la base de datos")
                return

            empresas = [empresa]
        else:
            empresas = obtener_empresas_pendientes(conn, args.limite, args.reprocesar)

        log.info(f"Empresas a procesar: {len(empresas)}")

        contadores = {"ok": 0, "sin_xbrl": 0, "sin_trimestres": 0, "inactiva": 0, "error": 0}

        for i, (empresa_id, cik, ticker, nombre) in enumerate(empresas, 1):
            try:
                resultado = enriquecer_empresa(conn, empresa_id, cik, args.ventana)

                # Cuento resultados por tipo
                tipo = resultado.split(":")[0]
                contadores[tipo] = contadores.get(tipo, 0) + 1

                if i % 50 == 0:
                    log.info(
                        f"Progreso: {i}/{len(empresas)} — "
                        f"ok: {contadores['ok']} | "
                        f"inactivas: {contadores['inactiva']} | "
                        f"sin datos: {contadores['sin_xbrl']}"
                    )

                # Respeto el rate limit de la SEC
                time.sleep(PAUSA_ENTRE_REQUESTS)

            except Exception as e:
                log.error(f"Error procesando {ticker} (CIK {cik}): {e}")
                contadores["error"] += 1
                # No paro el proceso completo por un error individual
                continue

        log.info(
            f"\nResumen final:\n"
            f"  Correctas:       {contadores['ok']}\n"
            f"  Inactivas:       {contadores['inactiva']}\n"
            f"  Sin XBRL:        {contadores['sin_xbrl']}\n"
            f"  Sin trimestres:  {contadores['sin_trimestres']}\n"
            f"  Errores:         {contadores['error']}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
