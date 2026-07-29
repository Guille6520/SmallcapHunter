"""
ingesta_10q.py — descargo y guardo el texto narrativo (MD&A, Item 2)
del 10-Q más reciente de cada candidata final.

Este es el hueco que no habíamos cerrado: todo lo que tengo hasta ahora
es XBRL (números estructurados) y Form 4 (transacciones) — pero el RAG
que diseñé desde el principio necesita el texto narrativo real, que
viene de un endpoint distinto de la SEC y nunca lo había descargado.

Qué hago, paso a paso:
  1. Consulto el índice de filings de la empresa (mismo endpoint
     'submissions' que ya uso para el SIC) y busco su 10-Q más reciente
  2. Descargo el HTML del filing desde el archivo de EDGAR
  3. Intento aislar la sección "Item 2 — MD&A" con una búsqueda de
     patrones de encabezado. Si no la encuentro, guardo el documento
     completo como fallback — documentado, no oculto
  4. Emparejo el texto con el trimestre correcto de metricas_trimestrales
     (el trimestre cuya fecha_fin sea la más cercana anterior a la fecha
     de filing — un 10-Q siempre se presenta semanas después de que
     cierre el trimestre que describe)
  5. Guardo el texto en la columna texto_mda. El embedding lo dejo para
     un paso posterior aparte, porque depende de qué proveedor de
     embeddings se decida usar (Groq/Gemini no son OpenAI).

No genero ningún embedding aquí a propósito — separar "conseguir el
texto limpio" de "vectorizarlo" evita mezclar dos decisiones distintas
en un mismo script.

Cómo usarlo:
  python ingesta_10q.py
  python ingesta_10q.py --ticker NUVB
"""

import os
import re
import time
import logging
import argparse
from datetime import datetime

import requests
import psycopg2
from bs4 import BeautifulSoup
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

# La SEC exige un User-Agent identificable con contacto REAL, o
# empieza a devolver 403. Mismo patrón que en el resto del proyecto:
# el email sale del entorno, nunca hardcodeado.
SEC_HEADERS = {
    "User-Agent": f"SmallCapHunter {os.getenv('SEC_CONTACT_EMAIL', 'configura-SEC_CONTACT_EMAIL@en-tu-.env')}"
}
PAUSA_ENTRE_REQUESTS = 0.15

# Patrones de encabezado para localizar el inicio y fin del Item 2.
# Los filers redactan esto de formas ligeramente distintas, así que
# pruebo varias variantes en vez de una sola regex rígida.
# Los filers usan indistintamente el apóstrofo recto (') y el
# tipográfico curvo ('), que son caracteres Unicode distintos. Mi
# primera versión solo aceptaba el recto y fallaba en la mayoría de
# filings reales, que usan el curvo. Ahora acepto los dos, o ninguno
# (algunos filers omiten el posesivo por completo).
PATRON_INICIO_MDA = re.compile(
    r"item\s*2\s*[:.\-–—]?\s*management(?:['’‘´`]?s)?\s+discussion\s+and\s+analysis",
    re.IGNORECASE
)

# Patrón laxo: el título del MD&A SIN exigir "Item 2" pegado justo
# delante. Solo se usa si el patrón principal no encuentra NINGÚN
# candidato — cubre el caso más probable de fallo real, donde la
# extracción de HTML mete un número de página o un ancla de navegación
# entre el número de item y el título ("Item 2.\n\n17\n\nManagement's
# Discussion...") y el patrón principal nunca casa con nada.
PATRON_INICIO_MDA_LAXO = re.compile(
    r"management(?:['’‘´`]?s)?\s+discussion\s+and\s+analysis\s+of\s+financial\s+condition",
    re.IGNORECASE
)

PATRON_FIN_MDA = re.compile(
    r"item\s*3\s*[:.\-–—]?\s*quantitative\s+and\s+qualitative",
    re.IGNORECASE
)

# Límite de fin alternativo: algunas small caps saltan directas a
# "Item 4. Controls and Procedures" sin titular el Item 3 exactamente
# así. Antes esto caía siempre al recorte ciego de 30.000 caracteres;
# con esto, si Item 4 aparece antes, cierro ahí en vez de sobre-extenderme.
PATRON_FIN_MDA_ALT = re.compile(
    r"item\s*4\s*[:.\-–—]?\s*controls\s+and\s+procedures",
    re.IGNORECASE
)

# El patrón laxo (sin "Item 2" delante) tiene un riesgo real: enganchar
# una MENCIÓN de pasada al MD&A, no su encabezado. Es clásico en las
# notas del Item 1 ("...should be read in conjunction with Management's
# Discussion and Analysis... contained in the Company's annual report
# on Form 10-K"). Lo descubrí probando con BYRN: capturó justo esa
# frase y arrastró 111.961 caracteres de estados financieros y notas
# del Item 1 antes de llegar al MD&A real. Si el contexto justo antes
# del match contiene alguna de estas frases, descarto el candidato
# entero — no compito por hueco con una mención de pasada.
FRASES_MENCION_CRUZADA = (
    "read in conjunction with",
    "should be read together with",
    "in conjunction with our",
    "in conjunction with the",
    "contained in the company",
    "contained in the corresponding",
    "included in the company",
    "discussed in the company",
    "discussed under",
    "as set forth in",
    "see the discussion",
    "refer to the discussion",
    "please see",
    "as described in",
)


def _es_mencion_cruzada(texto_completo: str, inicio_match: int) -> bool:
    """
    True si los ~150 caracteres justo antes del match parecen una
    referencia de pasada al MD&A en vez de su encabezado real.
    """
    contexto_previo = texto_completo[max(0, inicio_match - 150):inicio_match].lower()
    return any(frase in contexto_previo for frase in FRASES_MENCION_CRUZADA)


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def obtener_ultimo_10q(cik: str):
    """
    Consulto el índice de filings de la SEC y devuelvo los datos del
    10-Q más reciente: accession number, fecha de filing, y el nombre
    del documento principal. Devuelvo None si no encuentro ninguno.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()

        recientes = data.get("filings", {}).get("recent", {})
        formularios = recientes.get("form", [])
        accessions = recientes.get("accessionNumber", [])
        fechas = recientes.get("filingDate", [])
        documentos = recientes.get("primaryDocument", [])

        for i, forma in enumerate(formularios):
            if forma == "10-Q":
                return {
                    "accession": accessions[i],
                    "fecha_filing": fechas[i],
                    "documento": documentos[i],
                }
        return None

    except Exception as e:
        log.warning(f"Error consultando filings de CIK {cik}: {e}")
        return None


def descargar_y_extraer_mda(cik: str, accession: str, documento: str) -> tuple:
    """
    Descargo el HTML del filing y extraigo el texto del Item 2 (MD&A).
    Devuelvo (texto, encontrado_seccion_especifica).

    El patrón de inicio suele aparecer DOS VECES en un 10-Q real: una
    en la tabla de contenidos (una línea corta, con un número de página
    justo antes del siguiente "Item 3") y otra en el cuerpo real del
    documento (con miles de caracteres de contenido real antes del
    siguiente "Item 3"). Si me quedara con la primera aparición sin más,
    capturaría casi siempre la tabla de contenidos — vacía de contenido
    útil — en vez del capítulo real.

    La solución: busco TODAS las combinaciones posibles de (inicio, fin)
    y me quedo con la que tenga el hueco de texto más grande entre
    medias. La tabla de contenidos siempre va a producir un hueco
    minúsculo comparado con el capítulo real.
    """
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
        texto_completo = soup.get_text(separator="\n")
        texto_completo = re.sub(r"\n\s*\n+", "\n\n", texto_completo)
        texto_completo = re.sub(r"[ \t]+", " ", texto_completo)

        inicios = list(PATRON_INICIO_MDA.finditer(texto_completo))
        patron_laxo = False
        if not inicios:
            patron_laxo = True
            inicios = [
                m for m in PATRON_INICIO_MDA_LAXO.finditer(texto_completo)
                if not _es_mencion_cruzada(texto_completo, m.start())
            ]

        if not inicios:
            return texto_completo[:50000], False

        mejor_hueco = -1
        mejor_seccion = None

        for inicio in inicios:
            fin = PATRON_FIN_MDA.search(texto_completo, pos=inicio.end())
            if not fin:
                fin = PATRON_FIN_MDA_ALT.search(texto_completo, pos=inicio.end())
            if fin:
                hueco = fin.start() - inicio.end()
                candidata = texto_completo[inicio.start():fin.start()]
            else:
                # Sin "Item 3" ni "Item 4" después de este inicio — corto
                # a un tamaño razonable y lo trato como candidata también
                hueco = 30000
                candidata = texto_completo[inicio.start():inicio.start() + 30000]

            if hueco > mejor_hueco:
                mejor_hueco = hueco
                mejor_seccion = candidata

        # Un hueco menor a 500 caracteres es casi con seguridad una
        # entrada de tabla de contenidos, no contenido real — no me fío
        # de ese resultado y caigo al fallback del documento completo.
        # Con el patrón laxo exijo más margen (800): al no anclar en
        # "Item 2", tiene más riesgo de enganchar una mención de pasada
        # al MD&A (una referencia cruzada desde otra sección) en vez del
        # encabezado real.
        umbral = 800 if patron_laxo else 500
        if mejor_hueco < umbral:
            return texto_completo[:50000], False

        return mejor_seccion.strip(), True

    except Exception as e:
        log.warning(f"Error descargando/parseando filing {accession}: {e}")
        return None, False


def encontrar_trimestre_correspondiente(conn, empresa_id: int, fecha_filing_str: str):
    """
    Busco el trimestre de metricas_trimestrales cuya fecha_fin sea la
    más cercana ANTERIOR a la fecha de filing. Un 10-Q se presenta
    semanas después de que cierre el trimestre que describe, así que
    nunca debería emparejarse con un trimestre posterior a su propia
    fecha de presentación.
    """
    fecha_filing = datetime.strptime(fecha_filing_str, "%Y-%m-%d").date()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select id, anio_fiscal, trimestre
            from metricas_trimestrales
            where empresa_id = %s and fecha_fin <= %s
            order by fecha_fin desc
            limit 1
            """,
            (empresa_id, fecha_filing)
        )
        return cur.fetchone()
    finally:
        cur.close()


def guardar_texto_mda(conn, metrica_id: int, texto: str):
    cur = conn.cursor()
    try:
        cur.execute(
            "update metricas_trimestrales set texto_mda = %s where id = %s",
            (texto, metrica_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar texto_mda para métrica {metrica_id}: {e}")
    finally:
        cur.close()


def obtener_candidatas_finales(conn, ticker: str = None) -> list:
    cur = conn.cursor()
    try:
        if ticker:
            cur.execute(
                "select id, cik, ticker from empresas where ticker = %s", (ticker,)
            )
        else:
            # Leo el corte de la tabla configuracion en vez de tenerlo
            # fijo aquí. El valor definitivo es 25 sobre 40 (con 30
            # pasaban solo ~26 empresas; con 25 pasan ~112 — prefiero
            # que el embudo fino lo hagan los agentes a perder una
            # explosiva por 2 puntos de pesos manuales). Si se ajusta,
            # se cambia el dato en la tabla, no este código.
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


def main():
    parser = argparse.ArgumentParser(
        description="Ingesta del texto MD&A del 10-Q más reciente de cada candidata"
    )
    parser.add_argument("--ticker", type=str, help="Procesa solo esta empresa")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        candidatas = obtener_candidatas_finales(conn, args.ticker)
        log.info(f"Empresas a procesar: {len(candidatas)}")

        con_seccion_exacta, con_fallback, sin_filing, errores = 0, 0, 0, 0

        for empresa_id, cik, ticker in candidatas:
            filing = obtener_ultimo_10q(cik)
            if not filing:
                log.warning(f"{ticker}: sin 10-Q encontrado")
                sin_filing += 1
                time.sleep(PAUSA_ENTRE_REQUESTS)
                continue

            texto, encontrado = descargar_y_extraer_mda(
                cik, filing["accession"], filing["documento"]
            )
            time.sleep(PAUSA_ENTRE_REQUESTS)

            if texto is None:
                errores += 1
                continue

            trimestre = encontrar_trimestre_correspondiente(
                conn, empresa_id, filing["fecha_filing"]
            )
            if not trimestre:
                log.warning(f"{ticker}: sin trimestre XBRL con el que emparejar el 10-Q")
                sin_filing += 1
                continue

            metrica_id, anio, trim = trimestre
            guardar_texto_mda(conn, metrica_id, texto)

            if encontrado:
                con_seccion_exacta += 1
                log.info(f"{ticker}: MD&A extraído ({anio} Q{trim}, {len(texto)} caracteres)")
            else:
                con_fallback += 1
                log.info(
                    f"{ticker}: sección MD&A no localizada, guardado documento "
                    f"completo como fallback ({anio} Q{trim}, {len(texto)} caracteres)"
                )

        log.info(
            f"\nResumen final:\n"
            f"  Sección MD&A aislada correctamente: {con_seccion_exacta}\n"
            f"  Fallback (documento completo):      {con_fallback}\n"
            f"  Sin 10-Q o sin trimestre para emparejar: {sin_filing}\n"
            f"  Errores:                            {errores}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
