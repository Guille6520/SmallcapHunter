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

Claude y Gemini se llaman de forma completamente independiente (dos votos
ciegos, nunca se enseñan el uno al otro) — así lo decidimos desde el
diseño original del proyecto. El hueco que hoy ocupa Claude lo tuvo antes
un modelo gratuito vía OpenRouter (Llama, luego GPT-OSS, luego Nemotron):
fallaba una parte relevante de las veces con JSON roto o el proveedor
saturado (visto y medido en las pasadas de sep-2026), así que lo
sustituí por Claude Haiku 4.5, de pago pero barato y fiable.

Al terminar cada análisis y guardarlo, mando un aviso por Telegram
(notificador_telegram.py). Si Telegram no está configurado, el análisis
funciona exactamente igual — el aviso es cortesía, no dependencia.

Cómo usarlo:
  python detective.py --ticker NUVB --modelo claude
  python detective.py --ticker NUVB --modelo gemini
"""

import os
import json
import time
import logging
import argparse

import psycopg2

from notificador_telegram import notificar_analisis
from embeddings import generar_embedding_consulta, vector_a_literal_pgvector
# El verificador vive en su propio módulo (sin dependencias pesadas) para
# que el orquestador pueda leer VERSION_PROTOCOLO sin importar los SDKs de
# los LLM. Lo reexporto aquí porque auditor.py y los tests lo importan de
# detective.
from errores_llm import (  # noqa: F401
    CATEGORIAS_ERROR, clasificar_error, describir_error, describir_error_json,
)
from verificador_citas import (  # noqa: F401
    verificar_citas, _normalizar_para_comparar, VERSION_PROTOCOLO,
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

# Trunco el MD&A a un tamaño razonable de tokens. Claude Haiku y Gemini
# Flash tienen ventanas de contexto grandes, pero no quiero pagar latencia
# ni coste por secciones de 50.000 caracteres cuando el fallback trajo el
# documento completo en vez de solo el MD&A.
MAX_CARACTERES_MDA = 12000

# Los eventos 8-K son cortos por naturaleza, pero el fallback puede
# traer el documento entero — trunco cada uno y limito cuántos entran
# al prompt. Cinco eventos recientes cuentan la historia entre
# trimestres de sobra; más allá es histórico que el MD&A ya recoge.
MAX_EVENTOS_8K = 5
MAX_CARACTERES_EVENTO = 3000

# Capa 4 (RAG): cuántos trimestres históricos similares traigo como
# few-shot, y cuánto de cada uno le muestro al modelo. Tres casos son
# suficientes para dar contexto de patrón sin inflar el prompt — el
# objetivo es "esto se parece a X", no sustituir el análisis con más
# texto que el propio MD&A que se está evaluando.
K_CASOS_SIMILARES_RAG = 3
MAX_CARACTERES_CASO_SIMILAR = 1500

# Qué significa cada item para que el LLM no tenga que adivinarlo del
# número — la numeración de la SEC no es exactamente autoexplicativa.
ITEMS_8K_DESCRIPCION = {
    "1.01": "acuerdo material definitivo",
    "1.02": "terminación de acuerdo material",
    "2.01": "adquisición o venta de activos",
    "3.02": "venta de acciones no registradas (posible dilución)",
    "5.02": "salida o nombramiento de directivos",
}

# $/token de entrada y salida, para estimar el coste real de cada llamada
# (tokens reales que devuelve el proveedor x precio publicado, no una
# media). Comprobado en claude.com/pricing el 28-sep-2026: Haiku 4.5 =
# $1/$5 por millón de tokens de entrada/salida. Gemini Flash sigue en el
# tier gratuito de GEMINI_API_KEY (sin facturación activada) -- si algún
# día se activa facturación en esa cuenta esto pasa a estar mal, la API
# no expone el tier de la cuenta, hay que actualizarlo a mano.
PRECIOS_USD_POR_TOKEN = {
    "claude": {"entrada": 1.0 / 1_000_000, "salida": 5.0 / 1_000_000},
    "gemini": {"entrada": 0.0, "salida": 0.0},
}


class RespuestaLLM(str):
    """
    El texto de la respuesta, tal cual lo devolvía siempre llamar_modelo()
    -- se comporta exactamente como el str que ya esperaba todo el que la
    consume (parsear_json_llm, slicing para logs, comparaciones...),
    porque ES un str, solo que con los tokens de entrada/salida y el motivo
    de fin colgados como atributos extra. Así puedo medir coste real y
    saber si el modelo se cortó por el tope de tokens sin tocar la firma
    de llamar_modelo() ni de ningún caller existente.

    motivo_fin: lo que dice el proveedor ("end_turn" / "max_tokens" en
    Claude, "STOP" / "MAX_TOKENS" en Gemini). None si no lo informó.
    """
    def __new__(cls, texto, tokens_entrada=None, tokens_salida=None, motivo_fin=None):
        obj = str.__new__(cls, texto or "")
        obj.tokens_entrada = tokens_entrada
        obj.tokens_salida = tokens_salida
        obj.motivo_fin = motivo_fin
        return obj

    @property
    def truncada(self) -> bool:
        """True si el modelo paró por el tope de tokens de salida."""
        return "max_tokens" in str(self.motivo_fin or "").lower()


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


def buscar_casos_similares_rag(conn, empresa_id: int, texto_mda: str, k: int = K_CASOS_SIMILARES_RAG) -> list:
    """
    Capa 4 (RAG): busco, por similitud semántica de embeddings (pgvector,
    distancia coseno), los trimestres históricos que más se parecen al
    MD&A que estoy analizando ahora mismo — de cualquier empresa, incluida
    la propia si tuvo trimestres previos ya embebidos.

    Es opcional por diseño, con el mismo criterio que los 8-K o los
    13D/G: si generar_embeddings_rag.py todavía no se ha ejecutado (no
    hay embeddings en la BD), o la API de embeddings falla o no está
    configurada, el Detective sigue funcionando exactamente igual que
    antes de que existiera la Capa 4 — devuelvo lista vacía en vez de
    romper el análisis.
    """
    if not texto_mda:
        return []

    try:
        vector_consulta = generar_embedding_consulta(texto_mda[:MAX_CARACTERES_MDA])
    except Exception as e:
        log.warning(f"RAG: no pude generar el embedding de consulta ({e}) — sigo sin casos similares")
        return []

    literal = vector_a_literal_pgvector(vector_consulta)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select e.ticker, e.nombre, mt.anio_fiscal, mt.trimestre, mt.texto_mda,
                   mt.embedding <=> %s::vector as distancia
            from metricas_trimestrales mt
            join empresas e on e.id = mt.empresa_id
            where mt.embedding is not null
              and mt.empresa_id != %s
              and mt.texto_mda is not null
            order by mt.embedding <=> %s::vector
            limit %s
            """,
            (literal, empresa_id, literal, k),
        )
        filas = cur.fetchall()
    except psycopg2.Error as e:
        conn.rollback()
        log.warning(f"RAG: consulta de similitud falló ({e}) — sigo sin casos similares")
        return []
    finally:
        cur.close()

    return [
        {
            "ticker": ticker, "nombre": nombre, "anio_fiscal": anio,
            "trimestre": trimestre, "texto_mda": texto, "distancia": float(distancia),
        }
        for ticker, nombre, anio, trimestre, texto, distancia in filas
    ]


def formatear_casos_similares_rag(casos: list) -> str:
    """
    Bloque de contexto con los casos históricos recuperados por RAG.
    Dejo explícito que es material de referencia y NO una fuente citable
    — el verificador de citas solo comprueba contra el texto_fuente_citas
    de la propia empresa, así que una cita sacada de aquí se marcaría
    (correctamente) como no verificada. Se lo digo al modelo para que no
    se confunda y acabe penalizado por mi propio contexto.
    """
    if not casos:
        return ""

    bloques = []
    for caso in casos:
        extracto = (caso["texto_mda"] or "")[:MAX_CARACTERES_CASO_SIMILAR]
        bloques.append(
            f"[{caso['ticker']} — {caso['nombre']}, T{caso['trimestre']} {caso['anio_fiscal']} "
            f"— similitud semántica: {(1 - caso['distancia']) * 100:.0f}%]\n{extracto}"
        )
    cuerpo = "\n\n".join(bloques)

    return f"""

CASOS HISTÓRICOS SIMILARES (Capa 4 — recuperados por similitud semántica de embeddings, no por palabras clave). Son de OTRAS empresas en OTROS momentos, para darte contexto de patrón — NO son fuente citable de esta empresa, así que no incluyas citas literales de este bloque en tu respuesta:
\"\"\"
{cuerpo}
\"\"\""""


def construir_prompt(contexto: dict, casos_similares_texto: str = "") -> str:
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
\"\"\"{formatear_eventos_8k(contexto)}{formatear_senales_mercado(contexto)}{casos_similares_texto}

Tu tarea:
1. Identifica un catalizador NO OBVIO que el texto sugiera (algo que el mercado podría no estar valorando todavía)
2. Construye una tesis de inversión breve (3-4 frases)
3. Lista los 3-4 PUNTOS FUERTES y los 3-4 PUNTOS DÉBILES concretos del negocio según los documentos — este análisis lo estudiará un inversor humano con información adicional que tú no tienes, así que sé específico y útil, no genérico
4. CITAS: para cada afirmación clave de tu tesis, incluye una cita corta (máximo 20 palabras) del texto MD&A, de los eventos 8-K o de los documentos 13D/G de arriba que la respalde.
   - "cita_literal": copia el texto EXACTO, carácter por carácter, en el idioma original del documento (inglés). NO lo traduzcas, no uses "..." ni combines fragmentos de sitios distintos, y no añadas referencias, fuentes ni corchetes. Cita frases completas, no cifras sueltas.
   - No cites los SCORES NUMÉRICOS de arriba: son cálculos nuestros, no texto de ningún documento. Para las compras de insiders sí puedes copiar la línea tal cual aparece en la lista.
   - "cita_es": la traducción al español de esa misma cita, para el inversor que leerá el análisis.
   Si no puedes citar el texto original, no hagas la afirmación.
5. Da un veredicto preliminar de interés para investigación (NO es consejo de inversión): MUY_INTERESANTE, INTERESANTE, o NADA_INTERESANTE

Responde ÚNICAMENTE con un JSON válido, sin texto antes ni después, redactado en español salvo el campo "cita_literal" (que va en el idioma original del documento), con esta estructura exacta:
{{
  "catalizador_no_obvio": "string",
  "tesis_inversion": "string",
  "puntos_fuertes": ["string", "string", "string"],
  "puntos_debiles": ["string", "string", "string"],
  "riesgos": ["string", "string"],
  "citas": [
    {{"afirmacion": "string en español", "cita_literal": "texto EXACTO del documento, sin traducir", "cita_es": "traducción al español de la cita"}}
  ],
  "veredicto_preliminar": "MUY_INTERESANTE|INTERESANTE|NADA_INTERESANTE"
}}"""


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
    uso = getattr(respuesta, "usage_metadata", None)
    candidatos = getattr(respuesta, "candidates", None) or []
    motivo = getattr(candidatos[0], "finish_reason", None) if candidatos else None
    return RespuestaLLM(
        respuesta.text,
        tokens_entrada=getattr(uso, "prompt_token_count", None) if uso else None,
        tokens_salida=getattr(uso, "candidates_token_count", None) if uso else None,
        motivo_fin=getattr(motivo, "name", motivo),
    )


def llamar_claude(prompt: str) -> str:
    """
    Llamo a Claude Haiku 4.5 vía la API de Anthropic. Ocupa el hueco que
    antes tenía un modelo gratuito vía OpenRouter (Llama, GPT-OSS,
    Nemotron): fallaba con JSON roto o 429/503 del proveedor en una
    parte relevante de los casos (medido en las pasadas de sep-2026), y
    de pago pero sin ese cuello de botella cuesta céntimos por llamada.

    Claude no tiene modo JSON forzado como Gemini: a veces envuelve la
    respuesta en una valla ```json. La quito aquí para que el caller
    reciba JSON limpio siempre.

    max_tokens=8000: con 2000 el Auditor devolvía el JSON cortado en 3 de
    10 llamadas, y con 4000 volvió a pasar en 2 (llm_calls registró
    tokens_salida == tope exacto en todas). Desde que las citas llevan
    también su traducción (cita_es) la media del Detective es ~3.000
    tokens y el máximo tocó 4000. Solo pagas los tokens que el modelo usa
    de verdad, así que subir el tope no encarece las respuestas que ya
    cabían.
    """
    from anthropic import Anthropic

    cliente = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    respuesta = cliente.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = respuesta.content[0].text.strip()

    if texto.startswith("```"):
        # Quito la primera línea (```json o ```) y la valla de cierre
        texto = texto.split("\n", 1)[1] if "\n" in texto else texto
        if texto.rstrip().endswith("```"):
            texto = texto.rstrip()[:-3]
    return RespuestaLLM(
        texto.strip(),
        tokens_entrada=respuesta.usage.input_tokens,
        tokens_salida=respuesta.usage.output_tokens,
        motivo_fin=respuesta.stop_reason,
    )


def llamar_modelo(modelo: str, prompt: str) -> str:
    """Despacho único: todos los scripts llaman a los LLM por aquí."""
    if modelo == "gemini":
        return llamar_gemini(prompt)
    if modelo == "claude":
        return llamar_claude(prompt)
    raise ValueError(f"Modelo desconocido: {modelo}")


def registrar_llamada_llm(*, origen: str, modelo: str, ticker: str = None,
                           tokens_entrada: int = None, tokens_salida: int = None,
                           segundos: float, exito: bool, tipo_error: str = None):
    """
    Deja constancia de cada llamada real a un LLM en llm_calls: coste,
    latencia y tasa de error por modelo, con datos reales del propio
    sistema en vez de tener que mirar a mano el panel de cada proveedor
    -- lo hicimos así, a mano, durante días, para decidir si cambiar de
    modelo (ver benchmark_modelos.py, que no vive en este repo).

    Nunca debe romper el flujo real: si falla el propio registro, lo
    aviso en el log y sigo -- un problema de observabilidad no puede
    tirar un análisis que sí funcionó.
    """
    precios = PRECIOS_USD_POR_TOKEN.get(modelo)
    coste = None
    if precios and tokens_entrada is not None and tokens_salida is not None:
        coste = tokens_entrada * precios["entrada"] + tokens_salida * precios["salida"]

    try:
        conn = conectar_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                insert into llm_calls (
                    origen, modelo, ticker, tokens_entrada, tokens_salida,
                    latencia_segundos, coste_estimado_usd, exito, tipo_error
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (origen, modelo, ticker, tokens_entrada, tokens_salida,
                 round(segundos, 2), coste, exito, tipo_error)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"No pude registrar en llm_calls (origen={origen}, modelo={modelo}): {e}")


def parsear_json_llm(texto: str) -> dict:
    """
    Extraigo el PRIMER objeto JSON del texto, ignorando lo que haya
    alrededor. Claude a veces añade una frase de cortesía después del
    JSON ("Espero que este análisis...") y un json.loads directo casca
    con "Extra data" aunque el JSON esté perfecto. raw_decode para
    donde termina el objeto y no le importa lo que venga detrás.

    texto puede venir None -- lo he visto con Nemotron (vía OpenRouter)
    cuando el modelo agota el presupuesto de tokens razonando y no le
    queda nada para la respuesta final. Lo trato como el mismo tipo de
    fallo que un JSON roto (JSONDecodeError), no como un crash aparte,
    para que caiga en el except que ya tienen ejecutar_detective() y
    auditor.py en vez de reventar con un AttributeError sin explicación.
    """
    if not texto:
        raise json.JSONDecodeError("el modelo devolvió una respuesta vacía", texto or "", 0)
    inicio = texto.find("{")
    if inicio == -1:
        raise json.JSONDecodeError("sin objeto JSON en la respuesta", texto, 0)
    objeto, _ = json.JSONDecoder().raw_decode(texto[inicio:])
    return objeto


def ejecutar_detective(conn, ticker: str, modelo: str) -> dict: #Función principal que ejecuta el análisis del Detective para un ticker y modelo dados. Devuelve un diccionario con el contexto, la respuesta del LLM y la verificación de citas.
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        log.error(f"{ticker} no encontrado")
        return None
    if not contexto["texto_mda"]:
        log.error(f"{ticker} no tiene texto_mda — ejecuta ingesta_10q.py primero")
        return None

    casos_similares = buscar_casos_similares_rag(
        conn, contexto["empresa_id"], contexto["texto_mda"]
    )
    if casos_similares:
        log.info(f"RAG: {len(casos_similares)} casos históricos similares encontrados para {ticker}")
    prompt = construir_prompt(contexto, formatear_casos_similares_rag(casos_similares))

    log.info(f"Llamando a {modelo} para {ticker}...")
    t0 = time.time()
    try:
        texto_respuesta = llamar_modelo(modelo, prompt)
    except Exception as e:
        registrar_llamada_llm(
            origen="detective", modelo=modelo, ticker=ticker,
            segundos=time.time() - t0, exito=False, tipo_error=describir_error(e),
        )
        raise

    try:
        respuesta_json = parsear_json_llm(texto_respuesta)
    except json.JSONDecodeError as e:
        registrar_llamada_llm(
            origen="detective", modelo=modelo, ticker=ticker,
            tokens_entrada=getattr(texto_respuesta, "tokens_entrada", None),
            tokens_salida=getattr(texto_respuesta, "tokens_salida", None),
            segundos=time.time() - t0, exito=False, tipo_error=describir_error_json(texto_respuesta, e),
        )
        log.error(
            f"El modelo no devolvió JSON válido "
            f"({'cortado por max_tokens' if getattr(texto_respuesta, 'truncada', False) else 'sin truncar'}): "
            f"{e}\nRespuesta cruda: {texto_respuesta[:500]}"
        )
        return None

    registrar_llamada_llm(
        origen="detective", modelo=modelo, ticker=ticker,
        tokens_entrada=getattr(texto_respuesta, "tokens_entrada", None),
        tokens_salida=getattr(texto_respuesta, "tokens_salida", None),
        segundos=time.time() - t0, exito=True,
    )

    # Marca de qué prompt y qué verificador produjeron esta fila: el
    # orquestador re-analiza las que sean de un protocolo anterior.
    respuesta_json["version_protocolo"] = VERSION_PROTOCOLO

    # Verifico contra el MD&A Y los eventos 8-K juntos: el prompt ofrece
    # los dos como fuente citable, así que la verificación tiene que
    # cubrir los dos o penalizaría citas legítimas del 8-K.
    verificacion = verificar_citas(
        respuesta_json.get("citas", []), texto_fuente_citas(contexto),
        transacciones=contexto["transacciones"],
    )

    log.info(
        f"{ticker} ({modelo}): veredicto preliminar = "
        f"{respuesta_json.get('veredicto_preliminar')} | "
        f"citas soportadas: {verificacion['citas_soportadas']}/{verificacion['citas_totales']} "
        f"({verificacion['porcentaje_soportado']}%), "
        f"literales: {verificacion['citas_verificadas']} ({verificacion['porcentaje_verificado']}%)"
    )
    if verificacion["alucinacion_detectada"]:
        log.warning(f"{ticker} ({modelo}): POSIBLE ALUCINACIÓN — menos del 50% de citas con soporte en el texto")
    elif verificacion["citas_debiles"]:
        log.warning(f"{ticker} ({modelo}): citas débiles — entre el 50% y el 80% con soporte")

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
            citas_verificadas=verificacion["citas_soportadas"],
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
    parser.add_argument("--modelo", choices=["claude", "gemini"], required=True)
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

