"""
ingesta_8k.py — descargo y guardo los eventos materiales (8-K) de los
últimos meses de cada candidata final.

El 10-Q me cuenta el trimestre; el 8-K me cuenta lo que pasa ENTRE
trimestres: contratos importantes, cambios de directivos, ampliaciones
de capital. Para una tesis de "fase pre-explosiva" eso es justo el tipo
de catalizador que el MD&A todavía no recoge.

El problema de los 8-K es el ruido: una small cap presenta 20-40 al año
y la mayoría es basura administrativa (resultados de juntas, press
releases que duplican el 10-Q). Por eso NO descargo el documento hasta
saber qué items trae: el índice 'submissions' de la SEC ya lista los
items de cada 8-K, así que filtro ahí — gratis — y solo bajo los
filings que traen algún item de los que me interesan (la lista vive en
la tabla configuracion, clave items_8k_relevantes).

Qué hago, paso a paso:
  1. Consulto el índice de filings de la empresa (mismo endpoint
     'submissions' que ya uso en ingesta_10q.py)
  2. Me quedo con los 8-K de los últimos N meses (meses_ventana_8k)
     cuyos items crucen con los relevantes
  3. Descargo el HTML de cada filing UNA vez y extraigo la sección de
     cada item relevante con el mismo truco del "hueco más grande" que
     ya funciona en el MD&A. Si no aíslo la sección, guardo el documento
     recortado como fallback — documentado, no oculto
  4. Inserto una fila por (filing, item) en eventos_8k. El UNIQUE
     (accession_number, item) hace la ingesta idempotente: re-ejecutar
     no duplica nada
  5. El embedding lo dejo para un paso posterior aparte, igual que con
     texto_mda — la decisión del proveedor de embeddings es de la
     Capa 4 y no la quiero mezclar aquí

Cómo usarlo:
  python ingesta_8k.py
  python ingesta_8k.py --ticker NUVB
  python ingesta_8k.py --ticker NUVB --meses 6
"""

import os
import re
import time
import logging
import argparse
from datetime import datetime, timedelta

import requests
import psycopg2
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

# Mismo patrón que en el resto del proyecto: la SEC exige contacto real
# en el User-Agent o devuelve 403, y el email sale del entorno.
SEC_HEADERS = {
    "User-Agent": f"SmallCapHunter {os.getenv('SEC_CONTACT_EMAIL', 'configura-SEC_CONTACT_EMAIL@en-tu-.env')}"
}
PAUSA_ENTRE_REQUESTS = 0.15

# Si el fallback guarda el documento completo, lo recorto. Un 8-K es
# mucho más corto que un 10-Q — 15.000 caracteres cubren de sobra el
# cuerpo real sin arrastrar exhibits enteros.
MAX_CARACTERES_FALLBACK = 15000

# Valores por si la tabla configuracion no tiene las claves (BD creada
# con un schema anterior). Los mismos que inserta schema.sql.
ITEMS_RELEVANTES_DEFAULT = "1.01,1.02,2.01,3.02,5.02"
MESES_VENTANA_DEFAULT = 12

# Cualquier encabezado de item ("Item 5.02.") o el bloque de firmas:
# los dos sirven como frontera de fin de sección. El patrón de firmas
# existe porque el último item del documento no tiene un "Item" después.
PATRON_ITEM_GENERICO = re.compile(r"item\s*\d\.\d{2}", re.IGNORECASE)
PATRON_FIRMAS = re.compile(r"signature(?:s)?\s*\n|pursuant\s+to\s+the\s+requirements\s+of\s+the\s+securities\s+exchange\s+act", re.IGNORECASE)


def parsear_items(cadena) -> list:
    """
    Normalizo la lista de items tal y como viene del índice de la SEC
    ("1.01,9.01", a veces con espacios) o de la tabla configuracion.
    Devuelvo una lista limpia como ['1.01', '9.01'].
    """
    if not cadena:
        return []
    return [i.strip() for i in str(cadena).split(",") if i.strip()]


def construir_patron_item(item: str):
    """
    Regex para localizar el encabezado de un item concreto. Escapo el
    punto (re.escape) porque '1.01' sin escapar casaría también con
    '1x01' — improbable, pero gratis de evitar.
    """
    return re.compile(rf"item\s*{re.escape(item)}", re.IGNORECASE)


def extraer_item_8k(texto_completo: str, item: str) -> tuple:
    """
    Aíslo la sección de un item dentro del texto plano de un 8-K.
    Devuelvo (texto, encontrado_seccion_especifica).

    Reutilizo la idea del "hueco más grande" que ya funciona con el
    MD&A del 10-Q: si el item aparece varias veces (portada, índice,
    cuerpo), la aparición real es la que tiene más texto hasta la
    siguiente frontera. La frontera es el siguiente encabezado de item
    O el bloque de firmas — el último item del documento no tiene otro
    "Item" detrás y sin el patrón de firmas me llevaría los exhibits.
    """
    patron_inicio = construir_patron_item(item)

    inicios = list(patron_inicio.finditer(texto_completo))
    if not inicios:
        return texto_completo[:MAX_CARACTERES_FALLBACK], False

    mejor_hueco = -1
    mejor_seccion = None

    for inicio in inicios:
        fin_item = PATRON_ITEM_GENERICO.search(texto_completo, pos=inicio.end())
        fin_firmas = PATRON_FIRMAS.search(texto_completo, pos=inicio.end())

        # Me quedo con la frontera más cercana de las dos que existan
        candidatos_fin = [f.start() for f in (fin_item, fin_firmas) if f]
        if candidatos_fin:
            fin = min(candidatos_fin)
            hueco = fin - inicio.end()
            candidata = texto_completo[inicio.start():fin]
        else:
            # Sin frontera después — corto a un tamaño razonable
            hueco = 10000
            candidata = texto_completo[inicio.start():inicio.start() + 10000]

        if hueco > mejor_hueco:
            mejor_hueco = hueco
            mejor_seccion = candidata

    # Un hueco minúsculo es una mención de índice/portada, no la sección
    # real. El umbral es menor que en el MD&A (500) porque hay items
    # legítimamente cortos — un 5.02 de dos frases es normal.
    if mejor_hueco < 200:
        return texto_completo[:MAX_CARACTERES_FALLBACK], False

    return mejor_seccion.strip(), True


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def leer_configuracion_8k(conn) -> dict:
    """
    Leo los parámetros de la tabla configuracion, con los mismos valores
    de schema.sql como red de seguridad si las claves no existen (BD
    creada con la versión anterior del schema y sin migrar).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "select clave, valor from configuracion where clave in "
            "('items_8k_relevantes', 'meses_ventana_8k')"
        )
        valores = dict(cur.fetchall())
        return {
            "items": parsear_items(valores.get("items_8k_relevantes", ITEMS_RELEVANTES_DEFAULT)),
            "meses": int(valores.get("meses_ventana_8k", MESES_VENTANA_DEFAULT)),
        }
    finally:
        cur.close()


def obtener_8ks_relevantes(cik: str, items_relevantes: list, meses: int) -> list:
    """
    Consulto el índice de filings y devuelvo los 8-K de los últimos N
    meses que traen algún item relevante. El índice ya lista los items
    de cada 8-K — este filtro no cuesta ni una descarga de documento.

    Ignoro los 8-K/A (enmiendas) a propósito: corrigen un 8-K anterior
    con OTRO accession number, así que el UNIQUE no los deduplicaría y
    tendría el mismo evento dos veces con texto casi idéntico. Prefiero
    perder alguna corrección menor a duplicar eventos.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()

        recientes = data.get("filings", {}).get("recent", {})
        formularios = recientes.get("form", [])
        accessions = recientes.get("accessionNumber", [])
        fechas_filing = recientes.get("filingDate", [])
        fechas_evento = recientes.get("reportDate", [])
        documentos = recientes.get("primaryDocument", [])
        items_por_filing = recientes.get("items", [])

        fecha_corte = (datetime.now() - timedelta(days=meses * 30)).date()
        relevantes = set(items_relevantes)
        resultado = []

        for i, forma in enumerate(formularios):
            if forma != "8-K":
                continue

            fecha_filing = datetime.strptime(fechas_filing[i], "%Y-%m-%d").date()
            if fecha_filing < fecha_corte:
                # El índice viene ordenado del más reciente al más
                # antiguo: en cuanto salgo de la ventana, no hay nada
                # más que mirar.
                break

            # El campo items puede faltar en filings muy antiguos —
            # sin items declarados no puedo filtrar barato, lo salto.
            items_declarados = set(parsear_items(
                items_por_filing[i] if i < len(items_por_filing) else ""
            ))
            items_que_cruzan = sorted(items_declarados & relevantes)
            if not items_que_cruzan:
                continue

            # reportDate es la fecha del EVENTO; si falta, uso la de
            # presentación como aproximación (se presenta a los pocos días).
            fecha_evento = (
                fechas_evento[i]
                if i < len(fechas_evento) and fechas_evento[i]
                else fechas_filing[i]
            )

            resultado.append({
                "accession": accessions[i],
                "fecha_evento": fecha_evento,
                "documento": documentos[i],
                "items": items_que_cruzan,
            })

        return resultado

    except Exception as e:
        log.warning(f"Error consultando filings de CIK {cik}: {e}")
        return []


def descargar_texto_8k(cik: str, accession: str, documento: str):
    """
    Descargo el HTML del documento principal del 8-K y lo devuelvo como
    texto plano limpio. Misma limpieza que en ingesta_10q.py.

    Limitación conocida y asumida: el contenido jugoso a veces vive en
    un exhibit aparte (EX-99.1) y el documento principal solo lo
    referencia. No persigo los exhibits — multiplicaría las descargas y
    el item del cuerpo ya dice QUÉ pasó, que es lo que el Detective
    necesita. Si algún caso concreto lo pide, el accession_number
    guardado permite ir al original a mano.
    """
    from bs4 import BeautifulSoup

    cik_sin_ceros = str(int(cik))
    accession_sin_guiones = accession.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_sin_ceros}/{accession_sin_guiones}/{documento}"
    )

    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.content, "html.parser")
        texto = soup.get_text(separator="\n")
        texto = re.sub(r"\n\s*\n+", "\n\n", texto)
        texto = re.sub(r"[ \t]+", " ", texto)
        return texto

    except Exception as e:
        log.warning(f"Error descargando 8-K {accession}: {e}")
        return None


def guardar_evento(conn, empresa_id: int, accession: str, item: str,
                   fecha_evento: str, texto: str, seccion_aislada: bool) -> bool:
    """
    Inserto el evento. on conflict do nothing sobre (accession_number,
    item) — la misma idempotencia que el loader de Form 4. Devuelvo si
    la fila era nueva, para que el resumen final distinga insertados de
    ya existentes.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            insert into eventos_8k (
                empresa_id, accession_number, item, fecha_evento,
                texto, seccion_aislada
            ) values (%s, %s, %s, %s, %s, %s)
            on conflict (accession_number, item) do nothing
            """,
            (empresa_id, accession, item, fecha_evento, texto, seccion_aislada)
        )
        insertado = cur.rowcount > 0
        conn.commit()
        return insertado
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar evento {accession}/{item}: {e}")
        return False
    finally:
        cur.close()


def obtener_candidatas_finales(conn, ticker: str = None) -> list:
    """
    Las mismas candidatas que ingesta_10q.py: solo las que superaron el
    corte de la Capa 2. Los 8-K de las descartadas no valen los tokens.
    """
    cur = conn.cursor()
    try:
        if ticker:
            cur.execute(
                "select id, cik, ticker from empresas where ticker = %s", (ticker,)
            )
        else:
            cur.execute("select valor from configuracion where clave = 'score_minimo_llm'")
            row = cur.fetchone()
            corte = int(row[0]) if row else 25

            cur.execute(
                """
                select distinct e.id, e.cik, e.ticker
                from empresas e
                join auditorias a on a.empresa_id = e.id
                where a.score_total >= %s and a.veredicto is null
                """,
                (corte,)
            )
        return cur.fetchall()
    finally:
        cur.close()


def procesar_empresa(conn, empresa_id: int, cik: str, ticker: str,
                     items_relevantes: list, meses: int) -> dict:
    """
    Proceso todos los 8-K relevantes de una empresa. Descargo cada
    documento UNA vez aunque traiga varios items relevantes — la
    extracción por item es local, la descarga es lo caro.
    """
    contadores = {"insertados": 0, "ya_existian": 0, "fallback": 0, "errores": 0}

    filings = obtener_8ks_relevantes(cik, items_relevantes, meses)
    time.sleep(PAUSA_ENTRE_REQUESTS)

    if not filings:
        log.info(f"{ticker}: sin 8-K relevantes en la ventana")
        return contadores

    for filing in filings:
        texto_completo = descargar_texto_8k(cik, filing["accession"], filing["documento"])
        time.sleep(PAUSA_ENTRE_REQUESTS)

        if texto_completo is None:
            contadores["errores"] += 1
            continue

        for item in filing["items"]:
            texto_item, aislado = extraer_item_8k(texto_completo, item)
            if not aislado:
                contadores["fallback"] += 1

            nuevo = guardar_evento(
                conn, empresa_id, filing["accession"], item,
                filing["fecha_evento"], texto_item, aislado
            )
            if nuevo:
                contadores["insertados"] += 1
                log.info(
                    f"{ticker}: item {item} del {filing['fecha_evento']} "
                    f"({len(texto_item)} caracteres"
                    f"{', fallback' if not aislado else ''})"
                )
            else:
                contadores["ya_existian"] += 1

    return contadores


def main():
    parser = argparse.ArgumentParser(
        description="Ingesta de eventos materiales (8-K) de cada candidata final"
    )
    parser.add_argument("--ticker", type=str, help="Procesa solo esta empresa")
    parser.add_argument("--meses", type=int, default=None,
                        help="Ventana en meses (por defecto, meses_ventana_8k de configuracion)")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion_8k(conn)
        meses = args.meses if args.meses else config["meses"]
        items = config["items"]
        log.info(f"Items relevantes: {items} | ventana: {meses} meses")

        candidatas = obtener_candidatas_finales(conn, args.ticker)
        log.info(f"Empresas a procesar: {len(candidatas)}")

        total = {"insertados": 0, "ya_existian": 0, "fallback": 0, "errores": 0}

        for empresa_id, cik, ticker in candidatas:
            contadores = procesar_empresa(conn, empresa_id, cik, ticker, items, meses)
            for clave in total:
                total[clave] += contadores[clave]

        log.info(
            f"\nResumen final:\n"
            f"  Eventos nuevos insertados:   {total['insertados']}\n"
            f"  Ya existían (idempotencia):  {total['ya_existian']}\n"
            f"  Con fallback (doc completo): {total['fallback']}\n"
            f"  Errores de descarga:         {total['errores']}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
