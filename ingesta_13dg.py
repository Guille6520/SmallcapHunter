"""
ingesta_13dg.py — descargo las participaciones >5% (Schedule 13D/13G)
de cada candidata final, y de paso detecto si tiene una shelf
registration activa (S-3 / 424B).

Por qué esto importa para la tesis:
  - Un 13D/G es la versión institucional de mi cluster de insiders:
    un fondo declarando el 5%+ de una small cap es dinero grande con
    información y convicción. Si coincide en el tiempo con el cluster,
    la señal se refuerza desde fuera de la empresa.
  - Una shelf (S-3/424B) reciente es la otra cara: la empresa tiene
    la escopeta cargada para diluir. Compras de insiders con shelf
    activa pueden ser teatro pre-ampliación — el "filtro de compra
    cosmética" que estaba en el roadmap empieza por saber este dato.

Las dos señales salen de fuentes DISTINTAS, y esto lo aprendí a base
de obtener cero resultados con la primera versión:
  - Las shelf (S-3/424B) las presenta LA EMPRESA, así que sí están en
    su índice 'submissions' — el mismo que uso para el 10-Q y el 8-K.
  - Los 13D/G los presenta EL FONDO, no la empresa, y NO aparecen en
    el submissions de la empresa. Hay que ir al buscador full-text de
    EDGAR (efts.sec.gov), que indexa cada filing bajo el CIK del
    filer Y el del sujeto. De paso: desde finales de 2024 la SEC los
    renombró de "SC 13D/G" a "SCHEDULE 13D/G" (formato XML) — otro
    motivo por el que la búsqueda con el nombre viejo daba cero.

Nota sobre las enmiendas: los /A SÍ los guardo, al contrario que los
8-K/A. En un 13D la enmienda es información nueva de verdad — el fondo
subió o bajó su posición — no una corrección administrativa.

Nota sobre la calidad de la señal: un SCHEDULE 13G de BlackRock o
Vanguard es gestión pasiva de índices, no convicción activista. Guardo
el nombre del filer al principio del texto para que el agente pueda
hacer esa distinción — un 13D de un fondo activista pesa mucho más.

Cómo usarlo:
  python ingesta_13dg.py
  python ingesta_13dg.py --ticker ARRY
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

SEC_HEADERS = {
    "User-Agent": f"SmallCapHunter {os.getenv('SEC_CONTACT_EMAIL', 'configura-SEC_CONTACT_EMAIL@en-tu-.env')}"
}
PAUSA_ENTRE_REQUESTS = 0.15

# Del texto del 13D/G guardo solo el principio: la portada del schedule
# trae quién declara, cuántas acciones y el porcentaje — el resto son
# anexos legales que no aportan al análisis.
MAX_CARACTERES_13DG = 8000

# Solo los nombres RAÍZ, nuevos (desde finales de 2024) y viejos. Al
# buscador se le pasan sin el sufijo /A: los nombres base filtran por
# root_forms, que ya incluye las enmiendas. Si mezclas base y /A en la
# misma petición, la API los convierte en dos filtros en AND y deja
# fuera los schedules originales — lo comprobé contra la API real.
FORMULARIOS_13DG = ("SCHEDULE 13D", "SCHEDULE 13G", "SC 13D", "SC 13G")

# El buscador full-text de EDGAR. Filtra por CIK del sujeto y por
# formulario, y devuelve JSON ordenado del más reciente al más viejo.
URL_FTS = "https://efts.sec.gov/LATEST/search-index"

# Formularios que delatan una shelf: el S-3 registra la posibilidad de
# emitir, los 424B son los folletos de una emisión ya en marcha.
PREFIJOS_SHELF = ("S-3", "424B")

# El porcentaje declarado suele aparecer como "9.9%" cerca de la frase
# "percent of class". Ojo: en el formato estándar de la portada, entre
# la frase y el número hay OTRO número ("Represented by Amount in Row
# (11)"), así que el hueco tiene que permitir dígitos — mi primera
# versión los excluía y fallaba justo con el formato más común. El
# lazy {0,200}? hace que me quede con el primer "N%" tras la frase,
# que es el porcentaje real (el "(11)" no lleva % detrás).
PATRON_PCT = re.compile(
    r"percent\s+of\s+class.{0,200}?(\d{1,3}(?:[.,]\d{1,2})?)\s*%",
    re.IGNORECASE | re.DOTALL
)

# Los schedules nuevos (desde finales de 2024) son XML estructurado:
# el porcentaje viene en una etiqueta propia, sin el símbolo %. No
# apuesto por un nombre exacto de etiqueta (percentOfClass, aggregate
# AmountPercent... la SEC ha variado nombres entre versiones del
# schema): acepto cualquier etiqueta que contenga "percent" seguida
# de un número plausible. El filtro 0-100 de después descarta lo que
# no sea un porcentaje de verdad.
PATRON_PCT_XML = re.compile(
    r"<[^>/]*percent[^>]*>\s*(\d{1,3}(?:\.\d{1,2})?)\s*<",
    re.IGNORECASE
)


def extraer_pct_participacion(texto: str):
    """
    Intento sacar el porcentaje declarado del texto del schedule.
    Devuelvo None si no lo encuentro — el texto queda guardado igual
    y el agente puede leerlo, esto es solo azúcar estructurado.
    """
    if not texto:
        return None
    m = PATRON_PCT_XML.search(texto) or PATRON_PCT.search(texto)
    if not m:
        return None
    try:
        pct = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    # Un 13D/G declara entre el 0% y el 100% de la clase (el 0-5% pasa
    # en enmiendas de salida). Fuera de ese rango, el regex pescó otro
    # número (un tipo de interés, una fecha).
    if pct < 0 or pct > 100:
        return None
    return pct


def es_formulario_shelf(formulario: str) -> bool:
    """S-3, S-3/A, 424B3, 424B5... — cualquier variante de los dos prefijos."""
    if not formulario:
        return False
    return formulario.strip().upper().startswith(PREFIJOS_SHELF)


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def leer_configuracion_13dg(conn) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            "select clave, valor from configuracion where clave in "
            "('meses_ventana_13dg', 'meses_shelf_activa')"
        )
        valores = dict(cur.fetchall())
        return {
            "meses_13dg": int(valores.get("meses_ventana_13dg", 12)),
            "meses_shelf": int(valores.get("meses_shelf_activa", 12)),
        }
    finally:
        cur.close()


def buscar_shelf_y_nt(cik: str, meses_shelf: int):
    """
    Una sola pasada por el índice 'submissions' que devuelve dos fechas:
      - la del S-3/424B más reciente (shelf activa = escopeta de dilución)
      - la del Form NT más reciente (NT 10-Q / NT 10-K: "no puedo
        presentar mis resultados a tiempo"). Un retraso contable suele
        preceder a reestructuraciones o problemas de auditoría serios —
        es de las red flags más baratas y potentes que existen, y viene
        del índice que ya estaba consultando gratis.
    Devuelvo (fecha_shelf, fecha_nt), cualquiera puede ser None.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()

        recientes = data.get("filings", {}).get("recent", {})
        formularios = recientes.get("form", [])
        fechas_filing = recientes.get("filingDate", [])

        corte = (datetime.now() - timedelta(days=meses_shelf * 30)).date()
        fecha_shelf, fecha_nt = None, None

        for i, forma in enumerate(formularios):
            fecha = datetime.strptime(fechas_filing[i], "%Y-%m-%d").date()
            # El índice viene ordenado del más reciente al más antiguo
            if fecha < corte:
                break
            if fecha_shelf is None and es_formulario_shelf(forma):
                fecha_shelf = fechas_filing[i]
            elif fecha_nt is None and forma.strip().upper().startswith("NT 10"):
                fecha_nt = fechas_filing[i]
            if fecha_shelf and fecha_nt:
                break
        return fecha_shelf, fecha_nt

    except Exception as e:
        log.warning(f"Error consultando filings de CIK {cik}: {e}")
        return None, None


def buscar_13dg_fts(cik: str, meses: int) -> list:
    """
    Los 13D/G de la ventana, desde el buscador full-text de EDGAR.
    Es la única fuente donde aparecen bajo el CIK de la empresa SUJETO
    (el submissions solo lista lo que la empresa presenta ella misma —
    la lección de la primera versión, que devolvía cero).

    De la respuesta saco también el nombre del filer (display_names
    trae "[empresa, filer]") — con eso el agente puede distinguir un
    13G pasivo de Vanguard de un 13D activista de verdad.
    """
    desde = (datetime.now() - timedelta(days=meses * 30)).date().isoformat()
    try:
        r = requests.get(
            URL_FTS,
            params={
                "q": "",
                "forms": ",".join(FORMULARIOS_13DG),
                "ciks": cik,
                "startdt": desde,
                "enddt": datetime.now().date().isoformat(),
            },
            headers=SEC_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        resultado = []
        for hit in data.get("hits", {}).get("hits", []):
            fuente = hit.get("_source", {})
            accession = fuente.get("adsh")
            if not accession:
                continue

            # El documento principal viene en el _id como
            # "accession:nombre_de_archivo"
            id_completo = hit.get("_id", "")
            documento = id_completo.split(":", 1)[1] if ":" in id_completo else "primary_doc.xml"

            # display_names = [empresa sujeto, filer] — me quedo el filer
            nombres = fuente.get("display_names", [])
            filer = nombres[1] if len(nombres) > 1 else "filer desconocido"

            resultado.append({
                "accession": accession,
                "formulario": fuente.get("form", ""),
                "fecha": fuente.get("file_date"),
                "documento": documento,
                "filer": filer,
            })
        return resultado

    except Exception as e:
        log.warning(f"Error en el buscador FTS para CIK {cik}: {e}")
        return []


def descargar_texto_13dg(cik: str, accession: str, documento: str):
    """
    Descargo el schedule y devuelvo (texto_limpio, texto_crudo).
    Los dos hacen falta: el porcentaje de los schedules nuevos vive en
    una etiqueta XML (<percentOfClass>) que la limpieza destruye, pero
    el texto que guardo para el agente quiero que sea legible, sin tags.
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
        crudo = r.text
        soup = BeautifulSoup(r.content, "html.parser")
        texto = soup.get_text(separator="\n")
        texto = re.sub(r"\n\s*\n+", "\n\n", texto)
        texto = re.sub(r"[ \t]+", " ", texto)
        return texto[:MAX_CARACTERES_13DG], crudo
    except Exception as e:
        log.warning(f"Error descargando 13D/G {accession}: {e}")
        return None, None


def guardar_participacion(conn, empresa_id: int, filing: dict, texto: str, pct) -> bool:
    """on conflict sobre el accession — idempotente como todo lo demás."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            insert into participaciones_activistas (
                empresa_id, accession_number, formulario, fecha_evento,
                pct_participacion, texto
            ) values (%s, %s, %s, %s, %s, %s)
            on conflict (accession_number) do nothing
            """,
            (empresa_id, filing["accession"], filing["formulario"],
             filing["fecha"], pct, texto)
        )
        insertado = cur.rowcount > 0
        conn.commit()
        return insertado
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar 13D/G {filing['accession']}: {e}")
        return False
    finally:
        cur.close()


def actualizar_shelf(conn, empresa_id: int, fecha_shelf, fecha_nt):
    """
    Marco o desmarco la shelf y el NT. Desmarcar importa: si la ventana
    pasó sin S-3 (o sin NT) nuevo, el dato viejo sería una acusación
    injusta en el prompt del agente.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            update empresas set
                shelf_activa = %s,
                fecha_ultimo_shelf = %s,
                fecha_ultimo_nt = %s
            where id = %s
            """,
            (fecha_shelf is not None, fecha_shelf, fecha_nt, empresa_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"No pude actualizar shelf/NT de empresa {empresa_id}: {e}")
    finally:
        cur.close()


def obtener_candidatas_finales(conn, ticker: str = None) -> list:
    """Mismas candidatas que ingesta_10q.py e ingesta_8k.py."""
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


def main():
    parser = argparse.ArgumentParser(
        description="Ingesta de 13D/G y detección de shelf de cada candidata final"
    )
    parser.add_argument("--ticker", type=str, help="Procesa solo esta empresa")
    args = parser.parse_args()

    conn = conectar_db()

    try:
        config = leer_configuracion_13dg(conn)
        candidatas = obtener_candidatas_finales(conn, args.ticker)
        log.info(
            f"Empresas a procesar: {len(candidatas)} | ventana 13D/G: "
            f"{config['meses_13dg']}m | shelf: {config['meses_shelf']}m"
        )

        insertados, ya_existian, con_shelf, sin_nada = 0, 0, 0, 0

        for empresa_id, cik, ticker in candidatas:
            fecha_shelf, fecha_nt = buscar_shelf_y_nt(cik, config["meses_shelf"])
            time.sleep(PAUSA_ENTRE_REQUESTS)

            actualizar_shelf(conn, empresa_id, fecha_shelf, fecha_nt)
            if fecha_shelf:
                con_shelf += 1
                log.info(f"{ticker}: shelf activa (último S-3/424B: {fecha_shelf})")
            if fecha_nt:
                log.warning(f"{ticker}: RED FLAG — Form NT (retraso contable) del {fecha_nt}")

            schedules = buscar_13dg_fts(cik, config["meses_13dg"])
            time.sleep(PAUSA_ENTRE_REQUESTS)

            if not schedules:
                if not fecha_shelf:
                    sin_nada += 1
                continue

            for filing in schedules:
                texto, crudo = descargar_texto_13dg(cik, filing["accession"], filing["documento"])
                time.sleep(PAUSA_ENTRE_REQUESTS)
                if texto is None:
                    continue

                # El porcentaje primero del XML crudo (schedules nuevos),
                # con el texto limpio como fallback (formato viejo).
                pct = extraer_pct_participacion(crudo) or extraer_pct_participacion(texto)

                # El filer al principio del texto: es lo primero que el
                # agente debe ver para distinguir un índice pasivo de un
                # activista de verdad.
                texto_final = (
                    f"FILER: {filing['filer']} | {filing['formulario']} "
                    f"| {filing['fecha']}\n\n{texto}"
                )

                if guardar_participacion(conn, empresa_id, filing, texto_final, pct):
                    insertados += 1
                    pct_txt = f"{pct}%" if pct is not None else "% no extraído"
                    log.info(
                        f"{ticker}: {filing['formulario']} de {filing['filer']} "
                        f"({filing['fecha']}, {pct_txt})"
                    )
                else:
                    ya_existian += 1

        log.info(
            f"\nResumen final:\n"
            f"  Participaciones nuevas:      {insertados}\n"
            f"  Ya existían (idempotencia):  {ya_existian}\n"
            f"  Empresas con shelf activa:   {con_shelf}\n"
            f"  Sin 13D/G ni shelf:          {sin_nada}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
