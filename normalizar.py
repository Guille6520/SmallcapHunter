"""
normalizar.py — reglas únicas de limpieza de datos

Este módulo existe porque los datos de la SEC llegan sucios y desde
varios endpoints distintos (TSV trimestrales del Form 4, JSON de
companyfacts, índices de filings), y necesito que un dato quede
EXACTAMENTE igual venga de donde venga. Sin esto, un mismo CIK escrito
de dos formas produce empresas duplicadas o transacciones huérfanas.

(Nota histórica: la primera versión también limpiaba un CSV de Kaggle
como segunda fuente; esa rama se eliminó, pero las reglas de limpieza
siguen siendo las mismas y siguen viviendo en un solo sitio.)

Tanto loader_backfill.py como enriquecedor_xbrl.py importan de aquí.
Si cambio una regla de limpieza, la cambio en un solo sitio.
"""

import re
from datetime import datetime, date
from typing import Optional


def normalizar_cik(valor) -> Optional[str]:
    """
    Deja el CIK siempre como 10 dígitos con ceros a la izquierda.

    El problema que resuelve: el CIK puede llegar de muchas formas según
    el endpoint. A veces como número limpio ("320193"), y al pasar por
    pandas puede llegar como float leído de CSV ("320193.0"), con
    espacios, o ya con ceros. Un zfill ingenuo sobre "320193.0" produce
    "00320193.0", que es un CIK corrupto y nunca casa con nada.

    Aquí extraigo solo los dígitos antes de rellenar, así da igual el
    formato de entrada.
    """
    if valor is None:
        return None

    texto = str(valor).strip()

    # Si viene como float de un CSV ("320193.0"), me quedo con la parte
    # entera antes del punto. Si no hago esto, el "0" decimal se pega a
    # los dígitos y produce un CIK corrupto (320193.0 -> 3201930).
    if "." in texto:
        texto = texto.split(".")[0]

    # Ahora sí, extraigo solo los dígitos (elimina espacios, prefijo CIK, etc.)
    solo_digitos = re.sub(r"\D", "", texto)

    if not solo_digitos:
        return None

    # Un CIK real nunca tiene más de 10 dígitos. Si tiene más, algo
    # ha ido mal (dos CIK pegados, por ejemplo) y prefiero descartarlo.
    if len(solo_digitos) > 10:
        return None

    return solo_digitos.zfill(10)


def normalizar_fecha(valor) -> Optional[date]:
    """
    Convierte una fecha a objeto date, probando los formatos que usan
    las distintas fuentes.

    La SEC usa formato DD-MON-YYYY en los TSV (ej: "15-MAR-2023") y
    formato ISO (YYYY-MM-DD) en las APIs JSON. A veces vienen como
    datetime ya parseado de pandas.
    """
    if valor is None:
        return None

    # Si ya es un date o datetime, lo devuelvo directamente
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor

    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "nat", "none", ""):
        return None

    # Pruebo los formatos en orden de probabilidad
    formatos = [
        "%Y-%m-%d",        # ISO, el de las APIs JSON de la SEC
        "%d-%b-%Y",        # DD-MON-YYYY, el de los TSV de la SEC (15-MAR-2023)
        "%m/%d/%Y",        # formato US
        "%Y-%m-%d %H:%M:%S",  # ISO con hora
    ]
    for fmt in formatos:
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            continue

    return None


def validar_fecha_transaccion(
    fecha_tx: Optional[date],
    fecha_filing: Optional[date] = None
) -> Optional[date]:
    """
    Comprueba que una fecha de transacción tiene sentido.

    Descarto fechas imposibles:
    - Futuras (un insider no puede reportar una compra que no ha pasado)
    - Anteriores a 2009 (antes no había XBRL fiable y no me sirven)
    - Posteriores a la fecha de filing (imposible: no puedes reportar
      antes de que ocurra)

    Estos errores pasan de verdad — los filers meten mal la fecha a mano.
    """
    if fecha_tx is None:
        return None

    hoy = date.today()

    # Fecha futura: error de tecleo del filer
    if fecha_tx > hoy:
        return None

    # Demasiado antigua para mi análisis
    if fecha_tx.year < 2009:
        return None

    # La transacción no puede ser posterior al filing que la reporta
    if fecha_filing and fecha_tx > fecha_filing:
        return None

    return fecha_tx


def normalizar_precio(valor) -> Optional[float]:
    """
    Limpia un precio por acción.

    Devuelvo None si el precio es 0, negativo o NaN, en vez de 0.
    Un precio de 0 en un Form 4 no significa "gratis" — significa que
    la transacción no fue una compra de mercado (fue una adjudicación,
    una donación, etc.). Guardarlo como 0 haría que el importe_total
    sea 0 y que el ratio de convicción luego divida por cero.
    """
    if valor is None:
        return None
    try:
        precio = float(valor)
    except (ValueError, TypeError):
        return None

    import math
    if math.isnan(precio) or math.isinf(precio):
        return None

    if precio <= 0:
        return None

    # Un precio por acción superior a 1 millón es un error de filing.
    # La acción más cara del mundo (Berkshire Hathaway A) ronda los 600.000$.
    if precio > 1_000_000:
        return None

    return precio


def normalizar_acciones(valor) -> Optional[int]:
    """
    Limpia el número de acciones.

    Uso el valor absoluto porque algunas fuentes marcan las ventas con
    número negativo y otras usan el transaction_code para el signo.
    Yo ya distingo compra/venta con el code, así que aquí solo quiero
    la magnitud. Si mezclara signos tendría importes negativos sin querer.
    """
    if valor is None:
        return None
    try:
        acciones = float(valor)
    except (ValueError, TypeError):
        return None

    # NaN y infinito no son valores válidos
    import math
    if math.isnan(acciones) or math.isinf(acciones):
        return None

    if acciones == 0:
        return None

    resultado = int(abs(acciones))

    # Límite práctico: ninguna empresa real tiene más de 100 billones de acciones.
    # Valores mayores son errores de tecleo en el filing.
    # El BIGINT de PostgreSQL aguanta hasta ~9.2 billones pero pongo un
    # techo más conservador para filtrar basura.
    if resultado > 100_000_000_000_000:
        return None

    return resultado


def calcular_importe(acciones: Optional[int], precio: Optional[float]) -> Optional[float]:
    """
    Calcula el importe total solo si tengo ambos datos válidos.

    Si falta el precio (adjudicaciones, opciones sin precio de mercado),
    devuelvo None en lugar de 0. Así el scoring sabe que no puede calcular
    convicción para esa transacción, en vez de calcularla sobre un 0 falso.

    También descarto importes que desbordarían BIGINT en PostgreSQL.
    Pasa cuando el precio por acción es absurdamente alto (error de filing).
    """
    if acciones is None or precio is None:
        return None

    resultado = round(acciones * precio, 2)

    # Si el importe supera 100 billones es un error de datos en el filing
    if abs(resultado) > 100_000_000_000_000:
        return None

    return resultado


# Códigos de transacción del Form 4 según la SEC.
# Los que me importan de verdad son P (compra) y S (venta).
CODIGOS_VALIDOS = {"P", "S", "A", "M", "F", "G", "X", "D", "C", "E",
                   "H", "I", "J", "K", "L", "O", "U", "V", "W", "Z"}

def normalizar_codigo(valor) -> Optional[str]:
    """
    Normaliza el código de transacción a mayúscula y valida que sea
    uno de los conocidos. Los que me importan de verdad son:
      P = compra en mercado abierto (la señal que busco)
      S = venta
    El resto los guardo pero pesan distinto en el scoring.
    """
    if valor is None:
        return None
    codigo = str(valor).strip().upper()
    if codigo in CODIGOS_VALIDOS:
        return codigo
    return None
