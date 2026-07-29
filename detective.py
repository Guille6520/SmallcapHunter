"""
detective.py — el primer agente de la Capa 3.

Le doy al LLM el contexto numérico (los 4 scores de Capa 2), el resumen
de las compras de insiders, y el texto real del MD&A — y le pido que
construya una tesis de inversión: por qué esta empresa podría estar en
fase pre-explosiva, qué catalizador no obvio ve, y qué riesgos identifica.

La pieza más importante de este archivo no es el LLM — es el
verificador de citas. Un estudio reciente de 2026 encontró que varios
modelos líderes se inventan datos financieros con apariencia de
autoridad cuando el documento fuente tiene huecos. Por eso exijo que el
Detective cite frases o cifras concretas del texto, y luego compruebo
mecánicamente (sin otro LLM) que esas citas existen de verdad en el
texto_mda original. Si no existen, lo marco como alucinación — no me
fío de que el LLM se autoevalúe.

Groq y Gemini se llaman de forma completamente independiente (dos votos
ciegos, nunca se enseñan el uno al otro) — así lo decidimos desde el
diseño original del proyecto.

Al terminar cada análisis y guardarlo, mando un aviso por Telegram
(notificador_telegram.py). Si Telegram no está configurado, el análisis
funciona exactamente igual — el aviso es cortesía, no dependencia.

Cómo usarlo:
  python detective.py --ticker NUVB --modelo groq
  python detective.py --ticker NUVB --modelo gemini
"""

import os
import re
import json
import logging
import argparse
import unicodedata

import psycopg2

from notificador_telegram import notificar_analisis
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

# Trunco el MD&A a un tamaño razonable de tokens. La mayoría de modelos
# gratuitos/rápidos (Llama 3.3 70B en Groq, Gemini Flash) tienen ventanas
# de contexto grandes, pero no quiero pagar latencia ni coste por
# secciones de 50.000 caracteres cuando el fallback trajo el documento
# completo en vez de solo el MD&A.
MAX_CARACTERES_MDA = 12000

# Los eventos 8-K son cortos por naturaleza, pero el fallback puede
# traer el documento entero — trunco cada uno y limito cuántos entran
# al prompt. Cinco eventos recientes cuentan la historia entre
# trimestres de sobra; más allá es histórico que el MD&A ya recoge.
MAX_EVENTOS_8K = 5
MAX_CARACTERES_EVENTO = 3000

# Qué significa cada item para que el LLM no tenga que adivinarlo del
# número — la numeración de la SEC no es exactamente autoexplicativa.
ITEMS_8K_DESCRIPCION = {
    "1.01": "acuerdo material definitivo",
    "1.02": "terminación de acuerdo material",
    "2.01": "adquisición o venta de activos",
    "3.02": "venta de acciones no registradas (posible dilución)",
    "5.02": "salida o nombramiento de directivos",
}


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def obtener_contexto_empresa(conn, ticker: str) -> dict:
    """
    Reúno todo lo que el Detective necesita ver de una empresa:
    identificación, los 4 scores de Capa 2, el resumen de insiders que
    formaron el cluster, y el texto MD&A más reciente disponible.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select e.id, e.nombre, e.sector, e.market_cap_usd, e.bolsa
            from empresas e where e.ticker = %s
            """,
            (ticker,)
        )
        row = cur.fetchone()
        if not row:
            return None
        empresa_id, nombre, sector, market_cap, bolsa = row

        cur.execute(
            """
            select score_precio, score_conviccion, score_temporal,
                   score_catalizador, score_total
            from auditorias
            where empresa_id = %s and veredicto is null
            order by fecha_analisis desc limit 1
            """,
            (empresa_id,)
        )
        scores = cur.fetchone()

        cur.execute(
            """
            select nombre_insider, cargo, fecha_transaccion, importe_total
            from insider_transactions
            where empresa_id = %s and tipo_transaccion = 'P'
            order by fecha_transaccion desc limit 10
            """,
            (empresa_id,)
        )
        transacciones = cur.fetchall()

        cur.execute(
            """
            select anio_fiscal, trimestre, texto_mda
            from metricas_trimestrales
            where empresa_id = %s and texto_mda is not null
            order by fecha_fin desc limit 1
            """,
            (empresa_id,)
        )
        mda_row = cur.fetchone()

        # Los eventos materiales recientes (8-K). Son opcionales: si
        # ingesta_8k.py no se ha ejecutado o la empresa no tuvo eventos,
        # el Detective funciona igual que antes, solo con el MD&A.
        # El try existe para las BD creadas con el schema anterior, donde
        # la tabla eventos_8k todavía no existe — aviso y sigo, en vez de
        # romper un análisis que funcionaba antes de añadir los 8-K.
        try:
            cur.execute(
                """
                select item, fecha_evento, texto
                from eventos_8k
                where empresa_id = %s and texto is not null
                order by fecha_evento desc
                limit %s
                """,
                (empresa_id, MAX_EVENTOS_8K)
            )
            eventos_8k = cur.fetchall()
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            log.warning(
                "La tabla eventos_8k no existe — aplica la migración del "
                "schema (ver GUIA_PRUEBAS.md). Sigo solo con el MD&A."
            )
            eventos_8k = []

        # Participaciones >5% (13D/G) — misma lógica opcional que los 8-K.
        try:
            cur.execute(
                """
                select formulario, fecha_evento, pct_participacion, texto
                from participaciones_activistas
                where empresa_id = %s
                order by fecha_evento desc
                limit 3
                """,
                (empresa_id,)
            )
            activistas = cur.fetchall()
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            activistas = []

        # Short interest y shelf — columnas nuevas de empresas; si la BD
        # no está migrada, sigo sin ellas en vez de romper el análisis.
        try:
            cur.execute(
                """
                select shares_short, short_pct_float, fecha_short_interest,
                       shelf_activa, fecha_ultimo_shelf, fecha_ultimo_nt
                from empresas where id = %s
                """,
                (empresa_id,)
            )
            senales = cur.fetchone()
        except psycopg2.errors.UndefinedColumn:
            conn.rollback()
            senales = None

        return {
            "empresa_id": empresa_id,
            "ticker": ticker,
            "nombre": nombre,
            "sector": sector,
            "market_cap": market_cap,
            "bolsa": bolsa,
            "scores": scores,
            "transacciones": transacciones,
            "mda_anio_trim": (mda_row[0], mda_row[1]) if mda_row else None,
            "texto_mda": mda_row[2] if mda_row else None,
            "eventos_8k": eventos_8k,
            "activistas": activistas,
            "senales_mercado": senales,
        }
    finally:
        cur.close()


def formatear_eventos_8k(contexto: dict) -> str:
    """
    Bloque de texto con los eventos 8-K para el prompt. Devuelvo cadena
    vacía si no hay eventos — así el prompt no menciona una sección que
    no existe y el LLM no se siente obligado a comentarla.
    """
    eventos = contexto.get("eventos_8k") or []
    if not eventos:
        return ""

    bloques = []
    for item, fecha, texto in eventos:
        descripcion = ITEMS_8K_DESCRIPCION.get(item, "evento material")
        bloques.append(
            f"[Item {item} — {descripcion} — {fecha}]\n"
            f"{(texto or '')[:MAX_CARACTERES_EVENTO]}"
        )

    cuerpo = "\n\n".join(bloques)
    return f"""

EVENTOS MATERIALES RECIENTES (8-K), del más nuevo al más viejo — lo que pasó ENTRE trimestres y que el MD&A puede no recoger todavía:
\"\"\"
{cuerpo}
\"\"\""""


def texto_fuente_citas(contexto: dict) -> str:
    """
    La fuente contra la que verifico las citas: el MD&A más el texto de
    los eventos 8-K y de los 13D/G. Sin esto, una cita perfectamente
    fiel a uno de esos documentos se marcaría como alucinación por el
    simple hecho de no estar en el MD&A — el verificador castigaría al
    modelo por leer bien.
    """
    partes = [contexto.get("texto_mda") or ""]
    for _, _, texto in (contexto.get("eventos_8k") or []):
        if texto:
            partes.append(texto)
    for _, _, _, texto in (contexto.get("activistas") or []):
        if texto:
            partes.append(texto)
    # También el bloque de señales tal y como lo ve el modelo: si cita
    # el aviso de la shelf o el short interest (texto que le puse YO en
    # el prompt), eso no es inventarse nada — es leer lo que le di.
    # Sin esto, el verificador castigaba citas legítimas de esa sección.
    partes.append(formatear_senales_mercado(contexto))
    return "\n\n".join(partes)


def formatear_senales_mercado(contexto: dict) -> str:
    """
    Bloque con las señales de contexto de mercado: short interest,
    shelf registration y participaciones >5%. Igual que con los 8-K,
    si no hay nada que decir devuelvo cadena vacía y el prompt no
    menciona la sección.
    """
    lineas = []

    senales = contexto.get("senales_mercado")
    if senales:
        shares_short, pct_float, fecha_short, shelf, fecha_shelf, fecha_nt = senales
        if fecha_nt:
            lineas.append(
                f"- RED FLAG CONTABLE: la empresa presentó un Form NT el {fecha_nt} "
                "(no pudo entregar sus resultados a tiempo). Un retraso contable suele "
                "preceder a problemas de auditoría o reestructuraciones — pondera esto "
                "con máxima severidad, por encima de cualquier señal positiva."
            )
        if pct_float is not None:
            lineas.append(
                f"- Short interest: {float(pct_float) * 100:.1f}% del float en corto"
                + (f" (dato del {fecha_short})" if fecha_short else "")
                + ". Un short alto junto a insiders comprando significa que alguien está muy equivocado — valora quién tiene mejor información."
            )
        if shelf:
            lineas.append(
                f"- ATENCIÓN: shelf registration activa (S-3/424B del {fecha_shelf}). "
                "La empresa puede emitir acciones en cualquier momento — las compras de insiders "
                "podrían ser una señal cosmética previa a una dilución. Pondera este riesgo."
            )

    for formulario, fecha, pct, texto in (contexto.get("activistas") or []):
        pct_txt = f"{pct}% declarado" if pct is not None else "porcentaje no extraído"
        extracto = (texto or "")[:1500]
        lineas.append(
            f"- Participación >5% ({formulario}, {fecha}, {pct_txt}). Extracto:\n{extracto}"
        )

    if not lineas:
        return ""

    cuerpo = "\n".join(lineas)
    return f"""

SEÑALES DE MERCADO ADICIONALES (short interest, dilución potencial, accionistas >5%):
{cuerpo}"""


def construir_prompt(contexto: dict) -> str:
    """
    Construyo el prompt con instrucciones explícitas de citar texto
    literal — sin eso, no tengo nada que verificar después.
    """
    sp, sc, st, scat, stotal = contexto["scores"] or (0, 0, 0, 0, 0)

    resumen_insiders = "\n".join(
        f"  - {nombre} ({cargo or 'cargo no especificado'}): "
        f"${importe:,.0f} el {fecha}"
        for nombre, cargo, fecha, importe in contexto["transacciones"]
        if importe is not None
    ) or "  (sin detalle de importes disponible)"

    texto_mda = (contexto["texto_mda"] or "")[:MAX_CARACTERES_MDA]

    return f"""Eres un analista financiero escéptico revisando una posible small cap en fase pre-explosiva.

EMPRESA: {contexto['nombre']} ({contexto['ticker']})
Sector: {contexto['sector']}
Market cap actual: ${contexto['market_cap']:,.0f}
Bolsa: {contexto['bolsa']}

SCORES NUMÉRICOS YA CALCULADOS (0-10 cada uno, ya verificados, no los cuestiones):
  - Posición en rango 52 semanas: {sp}/10 (más alto = más cerca de mínimos)
  - Convicción de insiders (amplitud + importe): {sc}/10
  - Aceleración de crecimiento trimestral: {st}/10
  - Mejora de catalizador (márgenes/caja): {scat}/10
  - TOTAL: {stotal}/40

COMPRAS DE INSIDERS RECIENTES:
{resumen_insiders}

TEXTO MD&A DEL ÚLTIMO 10-Q (puede estar recortado o ser el documento completo si no se pudo aislar la sección exacta):
\"\"\"
{texto_mda}
\"\"\"{formatear_eventos_8k(contexto)}{formatear_senales_mercado(contexto)}

Tu tarea:
1. Identifica un catalizador NO OBVIO que el texto sugiera (algo que el mercado podría no estar valorando todavía)
2. Construye una tesis de inversión breve (3-4 frases)
3. Lista los 3-4 PUNTOS FUERTES y los 3-4 PUNTOS DÉBILES concretos del negocio según los documentos — este análisis lo estudiará un inversor humano con información adicional que tú no tienes, así que sé específico y útil, no genérico
4. CITAS: para cada afirmación clave de tu tesis, incluye una cita literal corta (máximo 20 palabras) del texto MD&A, de los eventos 8-K o de los documentos 13D/G de arriba que la respalde. Copia el texto EXACTO, sin añadir referencias, fuentes ni corchetes al final, y sin combinar fragmentos de sitios distintos. Si no puedes citar el texto original, no hagas la afirmación.
5. Da un veredicto preliminar de interés para investigación (NO es consejo de inversión): MUY_INTERESANTE, INTERESANTE, o NADA_INTERESANTE

Responde ÚNICAMENTE con un JSON válido, sin texto antes ni después, con esta estructura exacta:
{{
  "catalizador_no_obvio": "string",
  "tesis_inversion": "string",
  "puntos_fuertes": ["string", "string", "string"],
  "puntos_debiles": ["string", "string", "string"],
  "riesgos": ["string", "string"],
  "citas": [
    {{"afirmacion": "string", "cita_literal": "string exacta del texto"}}
  ],
  "veredicto_preliminar": "MUY_INTERESANTE|INTERESANTE|NADA_INTERESANTE"
}}"""


def llamar_groq(prompt: str) -> str:
    """
    Llamo a Llama 3.3 70B vía Groq. La librería groq sigue una interfaz
    muy similar a la de OpenAI (chat.completions.create).
    """
    from groq import Groq
    cliente = Groq(api_key=os.environ["GROQ_API_KEY"])
    respuesta = cliente.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return respuesta.choices[0].message.content


def llamar_gemini(prompt: str) -> str:
    """
    Llamo a Gemini Flash con el SDK nuevo (google-genai). El paquete
    anterior (google.generativeai) quedó descontinuado — Google dejó de
    darle soporte, así que uso el cliente unificado nuevo.
    """
    from google import genai
    from google.genai import types

    api_key = os.environ["GEMINI_API_KEY"].strip()
    cliente = genai.Client(api_key=api_key)

    respuesta = cliente.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return respuesta.text


def llamar_claude(prompt: str) -> str: # Funcion alternativa que llama a Claude Haiku 4.5 vía la API de Anthropic, si se configura MODELO_SECUNDARIO=claude en el .env.
    """
    Llamo a Claude Haiku 4.5 vía la API de Anthropic. Existe como
    alternativa de pago (céntimos) al segundo voto: la cuota gratuita
    de Gemini (20 llamadas/día) se queda corta para pasadas grandes o
    para el backtest histórico. Se activa con MODELO_SECUNDARIO=claude
    en el .env — sin tocar código.

    Claude no tiene modo JSON forzado como Groq/Gemini: a veces envuelve
    la respuesta en una valla ```json. La quito aquí para que el caller
    reciba JSON limpio siempre, venga del modelo que venga.
    """
    from anthropic import Anthropic

    cliente = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    respuesta = cliente.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = respuesta.content[0].text.strip()

    if texto.startswith("```"):
        # Quito la primera línea (```json o ```) y la valla de cierre
        texto = texto.split("\n", 1)[1] if "\n" in texto else texto
        if texto.rstrip().endswith("```"):
            texto = texto.rstrip()[:-3]
    return texto.strip()


def modelo_secundario() -> str:
    """
    El segundo voto ciego: gemini (gratis, por defecto) o claude (de
    pago, sin cuello de cuota). Lo decide el .env, no el código.
    """
    valor = os.getenv("MODELO_SECUNDARIO", "gemini").strip().lower()
    if valor not in ("gemini", "claude"):
        log.warning(f"MODELO_SECUNDARIO={valor!r} no reconocido — uso gemini")
        return "gemini"
    return valor


def llamar_modelo(modelo: str, prompt: str) -> str:
    """Despacho único: todos los scripts llaman a los LLM por aquí."""
    if modelo == "groq":
        return llamar_groq(prompt)
    if modelo == "gemini":
        return llamar_gemini(prompt)
    if modelo == "claude":
        return llamar_claude(prompt)
    raise ValueError(f"Modelo desconocido: {modelo}")


def parsear_json_llm(texto: str) -> dict:
    """
    Extraigo el PRIMER objeto JSON del texto, ignorando lo que haya
    alrededor. Claude a veces añade una frase de cortesía después del
    JSON ("Espero que este análisis...") y un json.loads directo casca
    con "Extra data" aunque el JSON esté perfecto. raw_decode para
    donde termina el objeto y no le importa lo que venga detrás.
    """
    inicio = texto.find("{")
    if inicio == -1:
        raise json.JSONDecodeError("sin objeto JSON en la respuesta", texto, 0)
    objeto, _ = json.JSONDecoder().raw_decode(texto[inicio:])
    return objeto


def _normalizar_para_comparar(texto: str) -> str:
    """
    Normalizo antes de comparar: minúsculas, sin acentos, espacios
    colapsados. El LLM puede citar con capitalización o espaciado
    ligeramente distinto al original sin que eso sea "inventarse" nada.

    Ojo con los apóstrofos: el recto (') sobrevive a la conversión a
    ASCII, pero el tipográfico curvo (') no tiene equivalente ASCII y
    desaparece con NFKD. Si el texto fuente usa uno y el LLM cita con
    el otro (algo frecuentísimo — "Dave & Buster's" en el 10-Q real
    suele llevar el curvo, y el LLM casi siempre escribe el recto),
    uno de los dos se queda con el apóstrofo y el otro no, y una cita
    perfectamente fiel falla la comparación por pura tipografía, no
    por contenido inventado. Elimino explícitamente todas las variantes
    de apóstrofo/comilla ANTES de la normalización Unicode, así los dos
    textos reciben el mismo tratamiento sin importar qué tipografía use
    cada uno.
    """
    for caracter in ["'", "’", "‘", '"', "“", "”"]:
        texto = texto.replace(caracter, "")
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = texto.lower()
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def verificar_citas(citas: list, texto_fuente: str) -> dict:
    """
    Compruebo, de forma determinista (sin otro LLM), si cada cita
    literal que el Detective afirma haber sacado del texto existe de
    verdad ahí. No confío en que el modelo se autoevalúe — comparo
    directamente contra el texto_mda real.

    Marco una cita como verificada si aparece casi textual en la fuente
    (permito pequeñas diferencias de espaciado/acentos/mayúsculas, pero
    no invención de contenido).
    """
    fuente_normalizada = _normalizar_para_comparar(texto_fuente)

    resultados = []
    verificadas = 0

    for cita in citas:
        cita_literal = cita.get("cita_literal", "")

        # Algunos modelos (Claude sobre todo) añaden la referencia al
        # final de la cita — "[8-K, 2026-07-07]" — como un académico
        # educado. Esa coletilla no está en el texto fuente y hacía
        # fallar citas perfectamente fieles. La recorto antes de
        # comparar; el contenido de la cita sigue verificándose entero.
        cita_literal = re.sub(r"(\s*\[[^\]]{0,120}\])+\s*$", "", cita_literal)

        cita_normalizada = _normalizar_para_comparar(cita_literal)

        # Cita vacía o demasiado corta no cuenta como verificable
        if len(cita_normalizada) < 8:
            resultados.append({**cita, "verificada": False, "motivo": "cita_demasiado_corta"})
            continue

        existe = cita_normalizada in fuente_normalizada
        if existe:
            verificadas += 1
        resultados.append({**cita, "verificada": existe})

    total = len(citas)
    pct_verificadas = (verificadas / total * 100) if total > 0 else 0

    # Si menos de la mitad de las citas son verificables, es una señal
    # fuerte de que el modelo está alucinando contenido, no citándolo.
    alucinacion_detectada = total > 0 and pct_verificadas < 50

    return {
        "citas_verificadas": verificadas,
        "citas_totales": total,
        "porcentaje_verificado": round(pct_verificadas, 1),
        "alucinacion_detectada": alucinacion_detectada,
        "detalle": resultados,
    }


def ejecutar_detective(conn, ticker: str, modelo: str) -> dict: #Función principal que ejecuta el análisis del Detective para un ticker y modelo dados. Devuelve un diccionario con el contexto, la respuesta del LLM y la verificación de citas.
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        log.error(f"{ticker} no encontrado")
        return None
    if not contexto["texto_mda"]:
        log.error(f"{ticker} no tiene texto_mda — ejecuta ingesta_10q.py primero")
        return None

    prompt = construir_prompt(contexto)

    log.info(f"Llamando a {modelo} para {ticker}...")
    texto_respuesta = llamar_modelo(modelo, prompt)

    try:
        respuesta_json = parsear_json_llm(texto_respuesta)
    except json.JSONDecodeError as e:
        log.error(f"El modelo no devolvió JSON válido: {e}\nRespuesta cruda: {texto_respuesta[:500]}")
        return None

    # Verifico contra el MD&A Y los eventos 8-K juntos: el prompt ofrece
    # los dos como fuente citable, así que la verificación tiene que
    # cubrir los dos o penalizaría citas legítimas del 8-K.
    verificacion = verificar_citas(
        respuesta_json.get("citas", []), texto_fuente_citas(contexto)
    )

    log.info(
        f"{ticker} ({modelo}): veredicto preliminar = "
        f"{respuesta_json.get('veredicto_preliminar')} | "
        f"citas verificadas: {verificacion['citas_verificadas']}/{verificacion['citas_totales']} "
        f"({verificacion['porcentaje_verificado']}%)"
    )
    if verificacion["alucinacion_detectada"]:
        log.warning(f"{ticker} ({modelo}): POSIBLE ALUCINACIÓN — menos del 50% de citas verificadas")

    return {
        "contexto": contexto,
        "respuesta_llm": respuesta_json,
        "verificacion_citas": verificacion,
    }


def guardar_resultado(conn, empresa_id: int, modelo: str, resultado: dict): #Funcion que guarda el resultado del análisis en la base de datos y notifica por Telegram.
    veredicto = resultado["respuesta_llm"].get("veredicto_preliminar", "INTERESANTE")
    if resultado["verificacion_citas"]["alucinacion_detectada"]:
        veredicto = "ALUCINACION"

    cur = conn.cursor()
    try:
        cur.execute(
            """
            insert into auditorias (
                empresa_id, veredicto, respuesta_llm, verificacion_citas, modelo_llm
            ) values (%s, %s, %s, %s, %s)
            """,
            (
                empresa_id, veredicto,
                json.dumps(resultado["respuesta_llm"]),
                json.dumps(resultado["verificacion_citas"]),
                modelo,
            )
        )
        conn.commit()

        # Aviso por Telegram DESPUÉS del commit — no quiero avisar de un
        # análisis que luego falló al guardarse. Si Telegram no está
        # configurado, notificar_analisis lo dice en el log y sigue.
        contexto = resultado["contexto"]
        verificacion = resultado["verificacion_citas"]
        notificar_analisis(
            agente="Detective",
            ticker=contexto["ticker"],
            nombre=contexto["nombre"],
            modelo=modelo,
            veredicto=veredicto,
            citas_verificadas=verificacion["citas_verificadas"],
            citas_totales=verificacion["citas_totales"],
            extra=resultado["respuesta_llm"].get("catalizador_no_obvio"),
        )
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar resultado del Detective: {e}")
    finally:
        cur.close()


def main(): #Función principal que se ejecuta al correr el script desde la línea de comandos.
    parser = argparse.ArgumentParser(description="Agente Detective de la Capa 3")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--modelo", choices=["groq", "gemini", "claude"], required=True)
    args = parser.parse_args()

    conn = conectar_db()
    try:
        resultado = ejecutar_detective(conn, args.ticker, args.modelo)
        if resultado:
            guardar_resultado(conn, resultado["contexto"]["empresa_id"], args.modelo, resultado)
            print(json.dumps(resultado["respuesta_llm"], indent=2, ensure_ascii=False))
    finally:
        conn.close() #Garantizo que la conexión se cierra aunque haya errores.


if __name__ == "__main__":
    main()

