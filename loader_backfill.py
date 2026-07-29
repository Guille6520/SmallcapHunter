"""
loader_backfill.py — carga el histórico de Form 4 en PostgreSQL

La SEC distribuye los datos en ZIPs trimestrales. Cada ZIP pesa 8-15 MB
y contiene 8 archivos TSV. Descargo el ZIP en memoria, extraigo solo los
3 que necesito y descarto el resto — sin guardar nada en disco.

URL base de los ZIPs:
  https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/
  Formato: {anio}q{trimestre}_form345.zip  (ej: 2023q1_form345.zip)

Nota: la primera versión tenía además una rama para cargar un CSV de
Kaggle. La eliminé — nunca llegó a ejecutarse contra datos reales (el
mapeo de columnas estaba inferido de la descripción del dataset) y los
TSV oficiales de la SEC cubren todo el rango que necesito.

Cómo usarlo:
  # Backfill completo (descarga de internet):
  python loader_backfill.py --desde 2015 --hasta 2026

"""

import os
import io
import math
import time
import zipfile
import logging
import argparse
import requests
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from normalizar import (
    normalizar_cik, normalizar_fecha, validar_fecha_transaccion,
    normalizar_precio, normalizar_acciones, calcular_importe,
    normalizar_codigo,
)
from dotenv import load_dotenv

# Cargo el .env de la carpeta si existe — así las claves no dependen de
# pegarlas a mano en cada sesión nueva de PowerShell.
load_dotenv()

# Configuro el log para ver qué está pasando sin printf por todas partes
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


# Credenciales. En producción van en variables de entorno, nunca hardcodeadas.
# Si no hay variable de entorno, usa estos valores para desarrollo local.
# 127.0.0.1 y no "localhost": con Docker Desktop en Windows, localhost
# puede resolver mal y la conexión falla de forma intermitente.
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# La SEC requiere un User-Agent con nombre y email REAL para acceso sin
# rate limit — con un email falso se arriesga un 403. Lo leo del entorno
# para no dejar mi email personal hardcodeado en el repositorio.
SEC_HEADERS = {
    "User-Agent": f"SmallCapHunter {os.getenv('SEC_CONTACT_EMAIL', 'configura-SEC_CONTACT_EMAIL@en-tu-.env')}",
    "Accept-Encoding": "gzip, deflate",
}

# URL correcta de los ZIPs de la SEC (verificada en julio 2026)
SEC_BASE_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets"

# Columnas que me interesan de los TSV de la SEC.
# Nombres exactos según FORM_345_metadata.json de la SEC.
COLS_SUBMISSION = [
    "ACCESSION_NUMBER",    # clave primaria del filing
    "FILING_DATE",         # cuándo se presentó el Form 4 (DD-MON-YYYY)
    "PERIOD_OF_REPORT",    # fecha del evento que origina el filing
    "ISSUERCIK",           # CIK de la empresa emisora
    "ISSUERNAME",          # nombre de la empresa
    "ISSUERTRADINGSYMBOL", # ticker
]

COLS_OWNER = [
    "ACCESSION_NUMBER",
    "RPTOWNERCIK",           # CIK del insider
    "RPTOWNERNAME",          # nombre del insider
    "RPTOWNER_RELATIONSHIP", # OFFICER, DIRECTOR, TENPERCENTOWNER, OTHER
    "RPTOWNER_TITLE",        # título: "Chief Executive Officer"
]

COLS_NONDERIV = [
    "ACCESSION_NUMBER",
    "NONDERIV_TRANS_SK",       # clave de secuencia de la SEC: junto con
                               # ACCESSION_NUMBER identifica una transacción
                               # única dentro del filing (un Form 4 puede
                               # reportar varias transacciones distintas)
    "SECURITY_TITLE",
    "TRANS_DATE",              # fecha (DD-MON-YYYY)
    "TRANS_CODE",              # P=compra, S=venta, A=adjudicación, M=opciones
    "TRANS_SHARES",
    "TRANS_PRICEPERSHARE",
    "TRANS_ACQUIRED_DISP_CD",  # A=adquirida, D=dispuesta
    "SHRS_OWND_FOLWNG_TRANS",  # posición total tras la operación
]

# Archivos dentro del ZIP que me interesan. El resto los ignoro.
# Definido aquí, después de las COLS, para que Python los encuentre.
ARCHIVOS_NECESARIOS = {
    "SUBMISSION.tsv":     COLS_SUBMISSION,
    "REPORTINGOWNER.tsv": COLS_OWNER,
    "NONDERIV_TRANS.tsv": COLS_NONDERIV,
}


def conectar_db():
    """Conexión simple. Si falla, el error es claro."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = False
        log.info(f"Conectado a {DB_CONFIG['dbname']} en {DB_CONFIG['host']}")
        return conn
    except psycopg2.OperationalError as e:
        log.error(f"No puedo conectar a PostgreSQL: {e}")
        log.error("Comprueba que el servidor está activo y las variables de entorno son correctas")
        raise


def descargar_zip_sec(anio, trimestre):
    """
    Descarga el ZIP de un trimestre de la SEC y extrae en memoria
    solo los 3 archivos que necesito. No guarda nada en disco.

    El ZIP pesa entre 8 y 15 MB — pequeño y rápido.
    El nombre del ZIP sigue el patrón: 2023q1_form345.zip
    """
    url = f"{SEC_BASE_URL}/{anio}q{trimestre}_form345.zip"
    log.info(f"Descargando {anio} Q{trimestre} desde la SEC ({url})")

    for intento in range(3):
        try:
            r = requests.get(url, headers=SEC_HEADERS, timeout=60)

            if r.status_code == 404:
                log.warning(f"ZIP no encontrado: {url}")
                return None

            if r.status_code == 429:
                espera = 60 * (intento + 1)
                log.warning(f"Rate limit SEC — espero {espera}s")
                time.sleep(espera)
                continue

            r.raise_for_status()

            # Descomprimo en memoria — no necesito guardar el ZIP en disco
            dfs = {}
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                nombres_en_zip = z.namelist()
                for nombre_archivo, columnas in ARCHIVOS_NECESARIOS.items():
                    # Busco el archivo ignorando mayúsculas/minúsculas
                    coincidencia = next(
                        (n for n in nombres_en_zip
                         if n.upper().endswith(nombre_archivo.upper())),
                        None
                    )
                    if not coincidencia:
                        log.warning(f"{nombre_archivo} no está en el ZIP")
                        dfs[nombre_archivo] = None
                        continue

                    with z.open(coincidencia) as f:
                        df = pd.read_csv(
                            f, sep="\t",
                            usecols=lambda c: c in columnas,
                            dtype=str,
                            low_memory=False,
                        )
                        df.columns = df.columns.str.lower()
                        dfs[nombre_archivo] = df
                        log.info(f"  {nombre_archivo}: {len(df):,} filas")

            return dfs

        except requests.exceptions.Timeout:
            log.warning(f"Timeout en intento {intento+1}/3")
            time.sleep(10 * (intento + 1))
        except Exception as e:
            log.error(f"Error descargando {url}: {e}")
            if intento == 2:
                return None
            time.sleep(5)

    return None


def leer_tsv_local(carpeta, nombre_archivo, columnas):
    """
    Leo un TSV ya descargado en una carpeta local, con exactamente el
    mismo tratamiento que recibiría si viniera del ZIP (mismas columnas,
    todo como texto, nombres en minúscula). Busco el archivo sin
    distinguir mayúsculas porque según cómo se descomprimiera el ZIP
    el nombre puede variar.
    """
    ruta = os.path.join(carpeta, nombre_archivo)
    if not os.path.exists(ruta):
        coincidencia = next(
            (f for f in os.listdir(carpeta)
             if f.upper() == nombre_archivo.upper()),
            None
        )
        if not coincidencia:
            log.warning(f"{nombre_archivo} no está en {carpeta}")
            return None
        ruta = os.path.join(carpeta, coincidencia)

    df = pd.read_csv(
        ruta, sep="\t",
        usecols=lambda c: c in columnas,
        dtype=str,
        low_memory=False,
    )
    df.columns = df.columns.str.lower()
    log.info(f"  {nombre_archivo}: {len(df):,} filas (local)")
    return df


def limpiar_transacciones_sec(df_sub, df_owner, df_tx):
    """
    Combino los tres TSV en una tabla plana lista para PostgreSQL.
    La SEC separa submission, owner y transacciones en archivos distintos —
    aquí los uno y me quedo con lo que necesito.
    """
    if df_sub is None or df_tx is None:
        return pd.DataFrame()

    # Normalizo los nombres de columna
    df_sub.columns = df_sub.columns.str.lower()
    df_tx.columns = df_tx.columns.str.lower()

    # El join principal es submission + transacciones
    df = df_tx.merge(df_sub, on="accession_number", how="left")

    # Añado el owner si está disponible
    if df_owner is not None:
        df_owner.columns = df_owner.columns.str.lower()
        # Un filing puede tener varios owners — me quedo con el primero
        # (el más relevante suele ser el que firmó)
        df_owner_primero = df_owner.drop_duplicates(subset="accession_number", keep="first")
        df = df.merge(df_owner_primero, on="accession_number", how="left")

    # Renombro columnas de la SEC a los nombres internos del schema.
    # Los nombres reales los saqué del FORM_345_metadata.json oficial.
    # Si la SEC los cambia en el futuro, solo toco este diccionario.
    renombrar_sec = {
        "issuercik":              "issuer_cik",
        "issuername":             "issuer_name",
        "issuertradingsymbol":    "issuer_ticker",
        "rptownername":           "rptowner_name",
        "rptowner_title":         "officer_title",
        "trans_date":             "transaction_date",
        "trans_code":             "transaction_code",
        "trans_shares":           "transaction_shares",
        "trans_pricepershare":    "transaction_price_per_share",
        "shrs_ownd_folwng_trans": "shares_owned_following_transaction",
        "trans_acquired_disp_cd": "adquirido_o_dispuesto",
        "nonderiv_trans_sk":      "trans_sk",
    }
    df = df.rename(columns={k: v for k, v in renombrar_sec.items() if k in df.columns})

    # Aplico las reglas de limpieza compartidas con normalizar.py
    df["issuer_cik"] = df["issuer_cik"].apply(normalizar_cik)
    df = df.dropna(subset=["issuer_cik"])

    df["transaction_code"] = df["transaction_code"].apply(normalizar_codigo)

    df["filing_date"] = df.get("filing_date").apply(normalizar_fecha)
    df["transaction_date"] = df["transaction_date"].apply(normalizar_fecha)

    # Valido cada fecha de transacción contra la de filing (una tx no puede
    # ocurrir después de reportarse) y descarto fechas imposibles.
    df["transaction_date"] = df.apply(
        lambda r: validar_fecha_transaccion(
            r["transaction_date"], r.get("filing_date")
        ),
        axis=1,
    )
    df = df.dropna(subset=["transaction_date"])

    # Precio y acciones con las reglas que evitan el 0 falso y el signo
    df["transaction_price_per_share"] = df["transaction_price_per_share"].apply(normalizar_precio)
    df["transaction_shares"] = df["transaction_shares"].apply(normalizar_acciones)

    # El importe solo si tengo ambos — si no, queda None y el scoring lo sabe
    df["importe_total"] = df.apply(
        lambda r: calcular_importe(
            r["transaction_shares"], r["transaction_price_per_share"]
        ),
        axis=1,
    )

    df["shares_owned_following_transaction"] = (
        df["shares_owned_following_transaction"].apply(normalizar_acciones)
    )

    return df


def cargar_trimestre_sec(conn, anio, trimestre, carpeta_local=None):
    """
    Carga un trimestre de Form 4 en PostgreSQL.

    Si carpeta_local apunta a una carpeta con los TSV ya descomprimidos,
    los lee de ahí. Si no, descarga el ZIP de la SEC y lo procesa en memoria.
    """
    if carpeta_local:
        log.info(f"Leyendo desde carpeta local: {carpeta_local}")
        dfs = {}
        for nombre, columnas in ARCHIVOS_NECESARIOS.items():
            dfs[nombre] = leer_tsv_local(carpeta_local, nombre, columnas)
    else:
        dfs = descargar_zip_sec(anio, trimestre)
        if not dfs:
            log.warning(f"Sin datos para {anio} Q{trimestre}")
            return 0
        # Pausa entre descargas para respetar el rate limit de la SEC
        time.sleep(1)

    df_sub   = dfs.get("SUBMISSION.tsv")
    df_owner = dfs.get("REPORTINGOWNER.tsv")
    df_tx    = dfs.get("NONDERIV_TRANS.tsv")

    df = limpiar_transacciones_sec(df_sub, df_owner, df_tx)

    if df.empty:
        log.warning(f"Sin transacciones válidas para {anio} Q{trimestre}")
        return 0

    return _insertar_en_db(conn, df, f"SEC {anio} Q{trimestre}")


def _insertar_en_db(conn, df, fuente):
    """
    Inserta las transacciones en las tablas empresas e insider_transactions.
    Primero crea o actualiza la empresa, luego inserta las transacciones.

    Uso on conflict para que el loader sea idempotente:
    puedo ejecutarlo varias veces sin duplicar filas.
    """
    if df.empty:
        return 0

    cur = conn.cursor()
    insertadas = 0

    try:
        # Primero sincronizo las empresas que aparecen en el dataset.
        # Solo inserto las que no existen — no sobreescribo datos que ya tengo.
        empresas_unicas = (
            df[["issuer_cik", "issuer_name", "issuer_ticker"]]
            .dropna(subset=["issuer_cik"])
            .drop_duplicates(subset="issuer_cik")
        )

        empresas_rows = [
            (
                row["issuer_cik"],
                # Uso _sanear_texto en vez de row.get(..., "") porque en una
                # Series de pandas, .get() con default SOLO se aplica si la
                # columna no existe — si existe pero el valor es NaN (campo
                # vacío del TSV), .get() devuelve ese NaN tal cual. Al pasar
                # ese NaN a psycopg2, terminaba insertándose como el string
                # literal "None" en la columna VARCHAR, en vez de NULL real.
                # Esto rompía después el script de yfinance: intentaba
                # buscar el ticker "NONE" en Yahoo y fallaba con 404.
                _sanear_texto(row.get("issuer_ticker")) or "",
                _sanear_texto(row.get("issuer_name")) or "",
            )
            for _, row in empresas_unicas.iterrows()
            if row["issuer_cik"]
        ]

        if empresas_rows:
            execute_values(
                cur,
                """
                insert into empresas (cik, ticker, nombre)
                values %s
                on conflict (cik) do nothing
                """,
                empresas_rows,
            )
            log.info(f"[{fuente}] Empresas sincronizadas: {len(empresas_rows)}")

        # Ahora cargo el mapa cik -> id para las foreign keys
        cur.execute("select cik, id from empresas")
        cik_a_id = {row[0]: row[1] for row in cur.fetchall()}

        # Inserto las transacciones en lotes de 1000 filas
        # para no hacer una sola INSERT gigante
        filas_tx = []
        for _, row in df.iterrows():
            cik = row.get("issuer_cik", "")
            empresa_id = cik_a_id.get(cik)

            if not empresa_id:
                continue  # empresa sin CIK válido, la salto

            # Saneo cada valor numérico justo antes de insertar. Esto es la
            # última línea de defensa: da igual lo que pasara antes en el
            # pipeline, aquí me aseguro de que nada fuera de rango llega a
            # PostgreSQL. Si un valor no cabe en BIGINT, lo dejo en None.
            filas_tx.append((
                empresa_id,
                _sanear_texto(row.get("rptowner_name")),
                _sanear_texto(row.get("officer_title") or row.get("rptowner_relationship")),
                _sanear_texto(row.get("transaction_code")),
                _sanear_adquirido_dispuesto(row.get("adquirido_o_dispuesto")),
                row.get("transaction_date"),
                _sanear_bigint(row.get("transaction_shares")),
                _sanear_numeric(row.get("transaction_price_per_share")),
                _sanear_importe(row.get("importe_total")),
                _sanear_bigint(row.get("shares_owned_following_transaction")),
                _sanear_texto(row.get("accession_number")),
                _sanear_bigint(row.get("trans_sk")),
            ))

            # Inserto en lotes para controlar la memoria
            if len(filas_tx) >= 1000:
                _insertar_batch_tx(cur, filas_tx)
                insertadas += len(filas_tx)
                filas_tx = []

        # Último batch (puede ser menor de 1000)
        if filas_tx:
            _insertar_batch_tx(cur, filas_tx)
            insertadas += len(filas_tx)

        conn.commit()
        log.info(f"[{fuente}] Transacciones insertadas: {insertadas:,}")
        return insertadas

    except Exception as e:
        conn.rollback()
        log.error(f"[{fuente}] Error al insertar — rollback: {e}")
        raise
    finally:
        cur.close()


def _sanear_bigint(valor):
    """
    Última defensa antes de insertar en una columna BIGINT de PostgreSQL.
    Convierto a int y descarto cualquier cosa que no quepa o no sea válida.
    El límite real de BIGINT es 9.223.372.036.854.775.807.
    """
    if valor is None:
        return None
    try:
        f = float(valor)
        if math.isnan(f) or math.isinf(f):
            return None
        n = int(f)
    except (ValueError, TypeError, OverflowError):
        return None
    # Límite estricto de BIGINT de PostgreSQL
    if abs(n) > 9_223_372_036_854_775_807:
        return None
    return n


def _sanear_numeric(valor):
    """
    Igual que _sanear_bigint pero para las columnas DECIMAL (precio).
    El precio_por_accion es DECIMAL(12,4) — máximo 8 dígitos enteros.
    """
    if valor is None:
        return None
    try:
        f = float(valor)
        if math.isnan(f) or math.isinf(f):
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    # DECIMAL(12,4) permite hasta 99.999.999,9999
    if abs(f) > 99_999_999:
        return None
    return round(f, 4)


def _sanear_adquirido_dispuesto(valor):
    """
    El campo TRANS_ACQUIRED_DISP_CD de la SEC solo puede ser 'A' (adquirida)
    o 'D' (dispuesta). Cualquier otra cosa la descarto para no romper el
    CHECK constraint del schema — más vale NULL que un valor inventado.
    """
    texto = _sanear_texto(valor)
    if texto in ("A", "D"):
        return texto
    return None


def _sanear_texto(valor):
    """
    Limpia campos de texto. Pandas convierte celdas vacías en NaN (float),
    que al insertarse queda como el texto "NaN". Aquí lo convierto a None
    de verdad para que en PostgreSQL quede NULL.
    """
    if valor is None:
        return None
    try:
        if isinstance(valor, float) and math.isnan(valor):
            return None
    except (TypeError, ValueError):
        pass
    texto = str(valor).strip()
    if texto.lower() in ("nan", "nat", "none", ""):
        return None
    return texto


def _sanear_importe(valor):
    """
    Para importe_total, que es DECIMAL(15,2) en el schema.
    Máximo 13 dígitos enteros: 9.999.999.999.999,99
    Un importe mayor es un error de datos — lo descarto.
    """
    if valor is None:
        return None
    try:
        f = float(valor)
        if math.isnan(f) or math.isinf(f):
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    if abs(f) > 9_999_999_999_999:
        return None
    return round(f, 2)


def _insertar_batch_tx(cur, filas):
    """
    on conflict (accession_number, trans_sk) do update — esta es la
    combinación real que identifica una transacción única del Form 4.
    Antes usaba do nothing sin conflict target, lo cual no hacía nada:
    sin un UNIQUE constraint real detrás, PostgreSQL no tenía ningún
    conflicto que detectar y cada re-ejecución duplicaba todo en silencio.
    Con do update, además, si la SEC corrige un dato en una enmienda,
    la fila se actualiza en vez de quedar con el valor antiguo.
    """
    execute_values(
        cur,
        """
        insert into insider_transactions (
            empresa_id, nombre_insider, cargo, tipo_transaccion,
            adquirido_o_dispuesto, fecha_transaccion, acciones,
            precio_por_accion, importe_total, acciones_tras_tx,
            accession_number, trans_sk
        )
        values %s
        on conflict (accession_number, trans_sk) do update set
            nombre_insider        = EXCLUDED.nombre_insider,
            cargo                 = EXCLUDED.cargo,
            tipo_transaccion      = EXCLUDED.tipo_transaccion,
            adquirido_o_dispuesto = EXCLUDED.adquirido_o_dispuesto,
            fecha_transaccion     = EXCLUDED.fecha_transaccion,
            acciones              = EXCLUDED.acciones,
            precio_por_accion     = EXCLUDED.precio_por_accion,
            importe_total         = EXCLUDED.importe_total,
            acciones_tras_tx      = EXCLUDED.acciones_tras_tx
        """,
        filas,
    )


def backfill_sec(conn, desde_anio, hasta_anio):
    """
    Descarga todos los trimestres de la SEC en el rango dado.
    """
    total = 0
    for anio in range(desde_anio, hasta_anio + 1):
        for q in range(1, 5):
            # Algunos Q4 recientes pueden no estar publicados todavía
            try:
                n = cargar_trimestre_sec(conn, anio, q)
                total += n
                # Pausa entre trimestres para no machacar la SEC
                time.sleep(1)
            except Exception as e:
                log.error(f"Error en {anio} Q{q}: {e} — continúo con el siguiente")
                continue

    log.info(f"Backfill SEC completo: {total:,} transacciones cargadas")
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Carga el histórico de Form 4 en PostgreSQL"
    )
    parser.add_argument(
        "--desde",
        type=int,
        default=2015,
        help="Año de inicio para el backfill SEC (default: 2015)"
    )
    parser.add_argument(
        "--hasta",
        type=int,
        default=2026,
        help="Año de fin para el backfill SEC (default: 2026)"
    )
    parser.add_argument(
        "--carpeta",
        type=str,
        help="Carpeta local con los TSV ya descargados (evita descargar de internet)"
    )
    args = parser.parse_args()

    conn = conectar_db()

    try:
        if args.carpeta:
            # Modo local: leo los TSV que ya tengo descargados
            log.info(f"Modo local: leyendo TSV desde {args.carpeta}")
            n = cargar_trimestre_sec(conn, None, None, carpeta_local=args.carpeta)
            log.info(f"Cargadas {n:,} transacciones desde carpeta local")
        else:
            log.info(f"Iniciando backfill SEC: {args.desde} a {args.hasta}")
            backfill_sec(conn, args.desde, args.hasta)

    finally:
        conn.close()
        log.info("Conexión cerrada")


if __name__ == "__main__":
    main()
