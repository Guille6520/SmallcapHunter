"""
auditor.py — el segundo agente de la Capa 3: verifica, no opina.

Recibe la tesis completa de un Detective y el texto original (MD&A, eventos 8-K,
señales de mercado) y comprueba qué parte de lo que dice está respaldada. NO da un
veredicto: el de la v1 (un juicio global sobre el análisis) era ruido, ver
verificaciones.py. El resultado es un SELLO por análisis (VERIFICADO / CON_AVISOS)
y los hallazgos concretos que lo justifican.

Dos partes, para que el modelo no pueda hacer trampa:

  A. CÓDIGO (verificacion_cifras.py): saca cada porcentaje e importe de la tesis y
     comprueba que aparezca en el texto original. Las cifras que no aparecen pueden
     ser errores o cálculos legítimos (un crecimiento derivado de dos cifras).
  B. MODELO, acotado: revisa afirmación por afirmación (respaldada / sin respaldo /
     contradicha) con una CITA LITERAL cada vez, adjudica las cifras que el código no
     encontró y señala riesgos que la tesis omite. El verificador de citas comprueba
     por código que cada cita existe en el documento: una cita inventada no cuenta. Y
     una cifra marcada solo se absuelve si el modelo enseña un cálculo cuyos operandos
     estén de verdad en el texto.

Cruzado a propósito: si el Detective fue Claude, el Auditor es Gemini, y viceversa.
Un modelo auditando su propio análisis tiende a confirmar su propio sesgo.

Reutilizo de detective.py todo lo que ya está probado: el contexto de la empresa, las
llamadas a Claude/Gemini y el verificador de citas determinista.

Al guardar cada auditoría mando un aviso por Telegram, igual que el Detective.

Cómo usarlo:
  # Audita automáticamente todos los análisis Detective ya guardados
  # para este ticker, con el modelo cruzado correspondiente:
  python auditor.py --ticker PLAY

  # O especifica cuál Detective auditar en concreto:
  python auditor.py --ticker PLAY --detective-modelo claude
"""

import json
import time
import logging
import argparse

from detective import (
    conectar_db, obtener_contexto_empresa, llamar_modelo,
    parsear_json_llm, verificar_citas, formatear_eventos_8k,
    formatear_senales_mercado, texto_fuente_citas, MAX_CARACTERES_MDA,
    registrar_llamada_llm, describir_error, describir_error_json,
)
from notificador_telegram import notificar_analisis
from verificacion_cifras import bloque_estructurado, cifras_sin_soporte
import verificaciones
from verificaciones import citas_del_auditor, valorar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# Cruce fijo: claude y gemini son los dos votos ciegos activos. "groq" ya
# no se ejecuta (era un modelo gratuito vía OpenRouter, retirado en
# sep-2026 por JSON roto y 429/503 frecuentes) pero queda como valor
# válido para poder seguir auditando análisis viejos guardados con ese
# nombre -- no los borro ni los reetiqueto, son historial real.
CRUCE_MODELOS = {"claude": "gemini", "gemini": "claude", "groq": "gemini"}


def modelo_opuesto(modelo: str) -> str:
    """El cruce automático: claude<->gemini. 'groq' (legado) se audita con
    gemini, que era el secundario por defecto cuando esas filas se
    generaron."""
    return CRUCE_MODELOS[modelo]


def obtener_analisis_detective(conn, empresa_id: int, modelo_detective: str = None) -> list:
    """
    Traigo el ÚLTIMO análisis del Detective de cada modelo para esta
    empresa. Los identifico por la firma del JSON (veredicto_preliminar):
    las filas de scoring (veredicto nulo) y las auditorías antiguas de la v1
    (que también llevan veredicto) no tienen esa firma. Y al traer solo la
    última de cada modelo, no gasto llamadas en análisis ya reemplazados por
    otros más nuevos.

    Sin modelo concreto solo considero claude y gemini: los análisis
    antiguos de "groq" solo se auditan si se piden explícitamente.
    Si ya existe un sello de este mismo par (Detective, Auditor),
    ejecutar_auditor lo salta con ya_auditado_por.
    """
    cur = conn.cursor()
    try:
        modelos = [modelo_detective] if modelo_detective else ["claude", "gemini"]
        cur.execute(
            """
            select distinct on (modelo_llm)
                   id, modelo_llm, respuesta_llm, verificacion_citas
            from auditorias
            where empresa_id = %s
              and modelo_llm = any(%s)
              and respuesta_llm ? 'veredicto_preliminar'
              and veredicto is not null
            order by modelo_llm, fecha_analisis desc
            """,
            (empresa_id, modelos)
        )
        return cur.fetchall()
    finally:
        cur.close()


def ya_auditado_por(conn, detective_auditoria_id: int, modelo_auditor: str) -> bool:
    """Compruebo si ya existe un sello de este análisis por este modelo (con la versión
    actual del Auditor), para no duplicar trabajo (y coste de API) si se relanza el
    script. Las auditorías de la v1 no cuentan: eran otra cosa."""
    return verificaciones.ya_verificado(conn, detective_auditoria_id, modelo_auditor)


def tesis_a_revisar(respuesta_detective: dict) -> str:
    """El texto de la tesis que se audita. NO lleva el veredicto del Detective: el
    Auditor no tiene que opinar sobre él, y verlo lo ancla."""
    riesgos = respuesta_detective.get("riesgos") or []
    if isinstance(riesgos, (list, tuple)):
        riesgos = "; ".join(str(r) for r in riesgos)
    return (
        f"  Catalizador identificado: {respuesta_detective.get('catalizador_no_obvio')}\n"
        f"  Tesis de inversión: {respuesta_detective.get('tesis_inversion')}\n"
        f"  Riesgos que señala: {riesgos}"
    )


def fuente_para_verificar(contexto: dict) -> str:
    """Lo que vale como fuente para el código y para las citas: los documentos (MD&A,
    8-K, 13D/G, señales) más los datos estructurados que también vio el Detective."""
    return texto_fuente_citas(contexto) + "\n\n" + bloque_estructurado(contexto)


def construir_prompt_auditor(contexto: dict, respuesta_detective: dict, sospechosas: list) -> str:
    """
    Le doy al Auditor el texto completo otra vez (no solo un resumen de lo que dijo el
    Detective) para que pueda comprobar cada afirmación contra el original. Y la lista de
    cifras que el código no ha encontrado tal cual, para que las adjudique.

    A diferencia del Auditor de la v1, no se le pide un veredicto ni un juicio global,
    y no ve el veredicto del Detective: se le pide comprobar, afirmación por afirmación,
    con una cita literal cada vez.
    """
    texto_mda = (contexto["texto_mda"] or "")[:MAX_CARACTERES_MDA]
    if sospechosas:
        lista = "\n".join(f"  {n}. «{s['cifra']}» en: ...{s['contexto']}..." for n, s in enumerate(sospechosas, 1))
    else:
        lista = "  (ninguna: todas las cifras de la tesis aparecen en el texto)"

    return f"""Eres un auditor financiero riguroso. Otro analista (el "Detective") ha escrito una tesis sobre una empresa. NO emitas un juicio global ni una opinión de inversión: tu trabajo es comprobar, afirmación por afirmación, si lo que dice la tesis está respaldado por el texto original.

EMPRESA: {contexto['nombre']} ({contexto['ticker']})

TESIS DEL DETECTIVE A REVISAR:
{tesis_a_revisar(respuesta_detective)}

TEXTO MD&A COMPLETO DEL 10-Q (comprueba contra él, no asumas que el Detective lo interpretó bien):
\"\"\"
{texto_mda}
\"\"\"{formatear_eventos_8k(contexto)}{formatear_senales_mercado(contexto)}

{bloque_estructurado(contexto)}

CIFRAS DE LA TESIS QUE UN PROGRAMA NO HA ENCONTRADO TAL CUAL EN EL TEXTO (puede que sean cifras derivadas correctamente, p. ej. un porcentaje calculado a partir de otras cifras del texto, o expresadas de otra forma; o que sean erróneas):
{lista}

Tu tarea:
1. AFIRMACIONES. Extrae de la tesis y del catalizador las 3 a 6 afirmaciones factuales más importantes. Para cada una indica su estado: RESPALDADA (el texto lo dice o lo implica claramente), SIN_RESPALDO (el texto no dice nada al respecto) o CONTRADICHA (el texto dice otra cosa). En "cita_literal" copia EXACTO, carácter por carácter, el texto original en inglés que lo demuestra (máximo 20 palabras; NO lo traduzcas, sin "...", sin corchetes ni referencias). Una afirmación SIN_RESPALDO no lleva cita.
2. CIFRAS. Para cada cifra de la lista anterior: DERIVADA_CORRECTA (se obtiene con un cálculo a partir de cifras del texto: escribe el cálculo con los números del texto), SIN_SOPORTE (no aparece ni se puede derivar del texto) o CONTRADICHA (el texto da otra cifra: copia la cita literal). Si la lista está vacía, devuelve una lista vacía.
3. RIESGOS OMITIDOS. Hasta 3 riesgos importantes que figuren en el texto y que la tesis no mencione, cada uno con su cita literal (mismas reglas: exacta, en el idioma original, sin traducir).

Responde ÚNICAMENTE con un JSON válido, sin texto antes ni después, redactado en español salvo el campo "cita_literal" (que va en el idioma original del documento), con esta estructura exacta:
{{
  "afirmaciones": [{{"afirmacion": "string", "estado": "RESPALDADA|SIN_RESPALDO|CONTRADICHA", "cita_literal": "string"}}],
  "cifras": [{{"cifra": "string", "estado": "DERIVADA_CORRECTA|SIN_SOPORTE|CONTRADICHA", "explicacion": "string breve", "cita_literal": "string"}}],
  "riesgos_omitidos": [{{"riesgo": "string", "cita_literal": "string"}}]
}}"""


def ejecutar_auditor(conn, ticker: str, modelo_detective: str = None):
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        log.error(f"{ticker} no encontrado")
        return []

    analisis_previos = obtener_analisis_detective(conn, contexto["empresa_id"], modelo_detective)
    if not analisis_previos:
        log.error(
            f"{ticker} no tiene ningún análisis Detective guardado — "
            f"ejecuta detective.py primero"
        )
        return []

    resultados = []

    for detective_id, modelo_original, respuesta_detective, _ in analisis_previos:
        if modelo_original not in CRUCE_MODELOS:
            log.warning(f"Modelo desconocido en fila {detective_id}: {modelo_original}")
            continue
        modelo_auditor = modelo_opuesto(modelo_original)

        if ya_auditado_por(conn, detective_id, modelo_auditor):
            log.info(
                f"{ticker}: el análisis de {modelo_original} (id {detective_id}) "
                f"ya fue verificado por {modelo_auditor} — salto"
            )
            continue

        # Parte A: el código marca las cifras de la tesis que no están en el texto.
        fuente = fuente_para_verificar(contexto)
        sospechosas = cifras_sin_soporte(tesis_a_revisar(respuesta_detective), fuente)
        prompt = construir_prompt_auditor(contexto, respuesta_detective, sospechosas)

        log.info(
            f"Verificando análisis de {modelo_original} (id {detective_id}) "
            f"con {modelo_auditor}; el código marca {len(sospechosas)} cifra(s)..."
        )
        t0 = time.time()
        try:
            texto_respuesta = llamar_modelo(modelo_auditor, prompt)
        except Exception as e:
            registrar_llamada_llm(
                origen="auditor", modelo=modelo_auditor, ticker=ticker,
                segundos=time.time() - t0, exito=False, tipo_error=describir_error(e),
            )
            # Si la segunda auditoría falla (cuota, 503) no se pierde la primera, ya
            # pagada: esta se da por fallida y sigo con la siguiente. Cada análisis lo
            # audita un modelo distinto, así que el fallo de uno no tiene por qué ser
            # el del otro.
            log.error(f"{ticker}: la verificación de {modelo_original} (id {detective_id}) falló — {describir_error(e)}")
            continue

        try:
            respuesta_json = parsear_json_llm(texto_respuesta)
        except json.JSONDecodeError as e:
            registrar_llamada_llm(
                origen="auditor", modelo=modelo_auditor, ticker=ticker,
                tokens_entrada=getattr(texto_respuesta, "tokens_entrada", None),
                tokens_salida=getattr(texto_respuesta, "tokens_salida", None),
                segundos=time.time() - t0, exito=False, tipo_error=describir_error_json(texto_respuesta, e),
            )
            log.error(
                f"El auditor no devolvió JSON válido "
                f"({'cortado por max_tokens' if getattr(texto_respuesta, 'truncada', False) else 'sin truncar'}): {e}"
            )
            continue

        # Un JSON bien formado sin la lista de afirmaciones no sirve: sin ella no hay nada
        # que comprobar, y un sello VERIFICADO dado a una auditoría vacía sería mentira.
        if not isinstance(respuesta_json, dict) or not isinstance(respuesta_json.get("afirmaciones"), list) \
                or not respuesta_json["afirmaciones"]:
            registrar_llamada_llm(
                origen="auditor", modelo=modelo_auditor, ticker=ticker,
                tokens_entrada=getattr(texto_respuesta, "tokens_entrada", None),
                tokens_salida=getattr(texto_respuesta, "tokens_salida", None),
                segundos=time.time() - t0, exito=False,
                tipo_error="json_invalido: la auditoría no trae la lista 'afirmaciones'",
            )
            log.error(f"{ticker}: el auditor ({modelo_auditor}) no devolvió afirmaciones que comprobar — descarto la auditoría")
            continue

        registrar_llamada_llm(
            origen="auditor", modelo=modelo_auditor, ticker=ticker,
            tokens_entrada=getattr(texto_respuesta, "tokens_entrada", None),
            tokens_salida=getattr(texto_respuesta, "tokens_salida", None),
            segundos=time.time() - t0, exito=True,
        )

        # Parte B verificada por código: las citas del modelo contra el documento, y las
        # cifras absueltas contra su cálculo.
        verificacion = verificar_citas(citas_del_auditor(respuesta_json), fuente,
                                       transacciones=contexto["transacciones"])
        valoracion = valorar(respuesta_json, fuente, sospechosas, verificacion)

        log.info(
            f"{ticker}: {modelo_auditor} sobre {modelo_original} — {valoracion['sello']} "
            f"({valoracion['n_avisos']} aviso(s)) | "
            f"citas con soporte: {verificacion['citas_soportadas']}/{verificacion['citas_totales']}"
        )

        respuesta_json["version_auditor"] = verificaciones.VERSION_AUDITOR
        resultados.append({
            "empresa_id": contexto["empresa_id"],
            "ticker": contexto["ticker"],
            "nombre": contexto["nombre"],
            "modelo_auditor": modelo_auditor,
            "modelo_detective": modelo_original,
            "detective_auditoria_id": detective_id,
            "sello": valoracion["sello"],
            "resumen": valoracion,
            "respuesta_llm": respuesta_json,
            "verificacion_citas": verificacion,
        })

    return resultados


def guardar_resultado_auditor(conn, resultado: dict) -> bool:
    """Guardo el sello. Devuelvo True si quedó guardado y False si no, para que
    quien me llama no cuente como hecha una auditoría que se pagó y se perdió. El
    aviso de Telegram va aparte: si falla, el sello sigue guardado."""
    try:
        verificaciones.guardar(conn, resultado)
    except Exception as e:
        log.error(f"No pude guardar el sello del Auditor: {e}")
        return False

    # Aviso por Telegram tras el commit, igual que en detective.py.
    try:
        verificacion = resultado["verificacion_citas"]
        resumen = resultado["resumen"]
        notificar_analisis(
            agente="Auditor",
            ticker=resultado["ticker"],
            nombre=resultado["nombre"],
            modelo=resultado["modelo_auditor"],
            veredicto=resultado["sello"],
            citas_verificadas=verificacion["citas_soportadas"],
            citas_totales=verificacion["citas_totales"],
            extra=f"sobre el análisis de {resultado['modelo_detective']}: {resumen['n_avisos']} aviso(s)",
            etiqueta="Sello",
        )
    except Exception as e:
        log.warning(f"Sello guardado, pero el aviso por Telegram falló: {e}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Agente Auditor de la Capa 3 (cruzado): verifica, no opina")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--detective-modelo", choices=["claude", "gemini", "groq"], default=None,
                        help="'groq' solo tiene sentido para auditar análisis históricos; ya no se genera")
    args = parser.parse_args()

    conn = conectar_db()
    try:
        resultados = ejecutar_auditor(conn, args.ticker, args.detective_modelo)
        for resultado in resultados:
            if not guardar_resultado_auditor(conn, resultado):
                raise SystemExit(1)   # la auditoría se pagó y no se guardó: que se note
            print(json.dumps(resultado["resumen"], indent=2, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
