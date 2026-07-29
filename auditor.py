"""
auditor.py — el segundo agente de la Capa 3.

Recibe la tesis completa del Detective (catalizador, riesgos, citas ya
verificadas) MÁS el texto MD&A completo otra vez — no solo un resumen.
La idea es que el Auditor no se fíe de que el Detective leyó bien: que
vuelva a leer el texto original con ojo crítico y compruebe si la tesis
se sostiene, si hay algo que el Detective pasó por alto, y si el
veredicto preliminar es razonable.

Cruzado a propósito: si el Detective fue Groq, el Auditor es Gemini, y
viceversa. Un modelo auditando su propio análisis tiende a confirmar su
propio sesgo; cruzarlos da una segunda opinión de verdad independiente.

Reutilizo de detective.py todo lo que ya está probado: el contexto de
la empresa, las llamadas a Groq/Gemini, y el verificador de citas
determinista. No reinvento nada de eso aquí.

Al guardar cada auditoría mando un aviso por Telegram, igual que el
Detective — así desde el móvil se ve la conversación completa entre
los dos agentes sobre cada empresa.

Cómo usarlo:
  # Audita automáticamente todos los análisis Detective ya guardados
  # para este ticker, con el modelo cruzado correspondiente:
  python auditor.py --ticker PLAY

  # O especifica cuál Detective auditar en concreto:
  python auditor.py --ticker PLAY --detective-modelo groq
"""

import json
import logging
import argparse

from detective import (
    conectar_db, obtener_contexto_empresa, llamar_modelo, modelo_secundario,
    parsear_json_llm, verificar_citas, formatear_eventos_8k,
    formatear_senales_mercado, texto_fuente_citas, MAX_CARACTERES_MDA,
)
from notificador_telegram import notificar_analisis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

def modelo_opuesto(modelo: str) -> str:
    """
    El cruce automático, ahora dinámico: el opuesto de groq es el
    secundario configurado (gemini o claude), y el opuesto de cualquier
    secundario es groq. Así los análisis viejos hechos con gemini se
    siguen auditando bien aunque hoy el secundario sea claude.
    """
    return modelo_secundario() if modelo == "groq" else "groq"


def obtener_analisis_detective(conn, empresa_id: int, modelo_detective: str = None) -> list:
    """
    Traigo los análisis del Detective ya guardados para esta empresa.
    Los identifico por tener respuesta_llm no nulo y un veredicto real
    (no las filas de puro scoring numérico, que tienen veredicto NULL).
    Si ya existe una auditoría de este mismo par (Detective, Auditor),
    la excluyo — no quiero auditar dos veces el mismo análisis con el
    mismo auditor sin querer.
    """
    cur = conn.cursor()
    try:
        if modelo_detective:
            cur.execute(
                """
                select id, modelo_llm, respuesta_llm, verificacion_citas
                from auditorias
                where empresa_id = %s and modelo_llm = %s
                  and respuesta_llm is not null
                  and veredicto is not null
                order by fecha_analisis desc limit 1
                """,
                (empresa_id, modelo_detective)
            )
        else:
            cur.execute(
                """
                select id, modelo_llm, respuesta_llm, verificacion_citas
                from auditorias
                where empresa_id = %s
                  and respuesta_llm is not null
                  and veredicto is not null
                order by fecha_analisis desc
                """,
                (empresa_id,)
            )
        return cur.fetchall()
    finally:
        cur.close()


def ya_auditado_por(conn, detective_auditoria_id: int, modelo_auditor: str) -> bool:
    """Compruebo si ya existe una auditoría de este análisis por este modelo,
    para no duplicar trabajo (y coste de API) si se relanza el script."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select 1 from auditorias
            where modelo_llm = %s
              and respuesta_llm->>'detective_auditoria_id' = %s
            limit 1
            """,
            (modelo_auditor, str(detective_auditoria_id))
        )
        return cur.fetchone() is not None
    finally:
        cur.close()


def construir_prompt_auditor(contexto: dict, respuesta_detective: dict) -> str:
    """
    Le doy al Auditor el texto completo otra vez (no solo un resumen de
    lo que dijo el Detective) para que pueda encontrar cosas que el
    Detective pasó por alto, no solo revisar su razonamiento de oídas.

    Medí mi propio histórico de auditorías y encontré un sesgo real: el
    76.5% de las veces el Auditor EMPEORABA el veredicto del Detective,
    frente a un 2.9% de MEJORA — y se repetía igual con Groq de auditor
    que con Gemini, así que no era un modelo más duro que otro, era el
    prompt. Las tareas 1 y 2 de abajo solo le preguntaban por generosidad
    excesiva y riesgos omitidos — nunca por fortalezas infravaloradas.
    Añadí la tarea 3 para darle el mismo camino hacia MEJORA que ya tenía
    hacia EMPEORA. Un auditor que solo sabe buscar problemas no audita,
    solo desconfía.
    """
    texto_mda = (contexto["texto_mda"] or "")[:MAX_CARACTERES_MDA]

    return f"""Eres un auditor financiero escéptico. Un primer analista (el "Detective") ya revisó esta empresa y produjo una tesis de inversión. Tu trabajo es revisarla con ojo crítico, releyendo el texto original — no te fíes de que el Detective interpretó todo correctamente.

EMPRESA: {contexto['nombre']} ({contexto['ticker']})

TESIS DEL DETECTIVE A REVISAR:
  Catalizador identificado: {respuesta_detective.get('catalizador_no_obvio')}
  Tesis de inversión: {respuesta_detective.get('tesis_inversion')}
  Riesgos identificados: {respuesta_detective.get('riesgos')}
  Veredicto preliminar del Detective: {respuesta_detective.get('veredicto_preliminar')}

TEXTO MD&A COMPLETO DEL 10-Q (léelo tú mismo, no asumas que el Detective lo interpretó bien):
\"\"\"
{texto_mda}
\"\"\"{formatear_eventos_8k(contexto)}{formatear_senales_mercado(contexto)}

Tu tarea:
1. ¿La tesis del Detective se sostiene releyendo el texto? ¿Hay algo que interpretó de forma demasiado generosa, o demasiado conservadora?
2. ¿Hay algún riesgo importante en el texto, en los eventos 8-K o en las señales de mercado (short interest, shelf, 13D/G) que el Detective NO mencionó?
3. ¿Hay algún catalizador, fortaleza o dato positivo en el texto, en los eventos 8-K o en las señales de mercado que el Detective INFRAVALORÓ o no mencionó? Sé igual de riguroso buscando esto que buscando riesgos — un auditor que solo busca problemas no es un auditor, es un pesimista, y tu trabajo es corregir al Detective en cualquier dirección, no solo hacia abajo.
4. CITAS: si afirmas algo nuevo que el Detective no dijo (sea riesgo o fortaleza), cita literalmente (máx 20 palabras) el texto que lo respalda. Copia el texto EXACTO, sin añadir referencias, fuentes ni corchetes al final. Si no puedes citarlo, no lo afirmes.
5. Da tu propio veredicto de interés, independiente del preliminar (NO es consejo de inversión): MUY_INTERESANTE, INTERESANTE, o NADA_INTERESANTE
6. Indica explícitamente si tu veredicto CONFIRMA, MEJORA o EMPEORA el veredicto preliminar del Detective, y por qué

Responde ÚNICAMENTE con un JSON válido, sin texto antes ni después, con esta estructura exacta:
{{
  "tesis_se_sostiene": true o false,
  "riesgos_omitidos_por_detective": ["string"],
  "fortalezas_omitidas_por_detective": ["string"],
  "citas_nuevas": [
    {{"afirmacion": "string", "cita_literal": "string exacta del texto"}}
  ],
  "veredicto_auditor": "MUY_INTERESANTE|INTERESANTE|NADA_INTERESANTE",
  "relacion_con_veredicto_detective": "CONFIRMA|MEJORA|EMPEORA",
  "justificacion": "string breve explicando el cambio o confirmación"
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
        if modelo_original not in ("groq", "gemini", "claude"):
            log.warning(f"Modelo desconocido en fila {detective_id}: {modelo_original}")
            continue
        modelo_auditor = modelo_opuesto(modelo_original)

        if ya_auditado_por(conn, detective_id, modelo_auditor):
            log.info(
                f"{ticker}: el análisis de {modelo_original} (id {detective_id}) "
                f"ya fue auditado por {modelo_auditor} — salto"
            )
            continue

        prompt = construir_prompt_auditor(contexto, respuesta_detective)

        log.info(
            f"Auditando análisis de {modelo_original} (id {detective_id}) "
            f"con {modelo_auditor}..."
        )
        texto_respuesta = llamar_modelo(modelo_auditor, prompt)

        try:
            respuesta_json = parsear_json_llm(texto_respuesta)
        except json.JSONDecodeError as e:
            log.error(f"El auditor no devolvió JSON válido: {e}")
            continue

        # Misma fuente ampliada que el Detective: MD&A + eventos 8-K.
        verificacion = verificar_citas(
            respuesta_json.get("citas_nuevas", []), texto_fuente_citas(contexto)
        )

        veredicto_final = respuesta_json.get("veredicto_auditor", "INTERESANTE")
        if verificacion["alucinacion_detectada"]:
            veredicto_final = "ALUCINACION"

        log.info(
            f"{ticker}: auditoría de {modelo_auditor} sobre {modelo_original} — "
            f"{respuesta_json.get('relacion_con_veredicto_detective')} "
            f"(veredicto auditor: {veredicto_final}) | "
            f"citas nuevas verificadas: {verificacion['citas_verificadas']}/{verificacion['citas_totales']}"
        )

        respuesta_json["detective_auditoria_id"] = detective_id
        respuesta_json["modelo_detective_auditado"] = modelo_original

        resultados.append({
            "empresa_id": contexto["empresa_id"],
            "ticker": contexto["ticker"],
            "nombre": contexto["nombre"],
            "modelo_auditor": modelo_auditor,
            "veredicto_final": veredicto_final,
            "respuesta_llm": respuesta_json,
            "verificacion_citas": verificacion,
        })

    return resultados


def guardar_resultado_auditor(conn, resultado: dict):
    cur = conn.cursor()
    try:
        cur.execute(
            """
            insert into auditorias (
                empresa_id, veredicto, respuesta_llm, verificacion_citas, modelo_llm
            ) values (%s, %s, %s, %s, %s)
            """,
            (
                resultado["empresa_id"], resultado["veredicto_final"],
                json.dumps(resultado["respuesta_llm"]),
                json.dumps(resultado["verificacion_citas"]),
                resultado["modelo_auditor"],
            )
        )
        conn.commit()

        # Aviso por Telegram tras el commit, igual que en detective.py.
        verificacion = resultado["verificacion_citas"]
        relacion = resultado["respuesta_llm"].get("relacion_con_veredicto_detective")
        notificar_analisis(
            agente="Auditor",
            ticker=resultado["ticker"],
            nombre=resultado["nombre"],
            modelo=resultado["modelo_auditor"],
            veredicto=resultado["veredicto_final"],
            citas_verificadas=verificacion["citas_verificadas"],
            citas_totales=verificacion["citas_totales"],
            extra=f"{relacion} el veredicto del Detective" if relacion else None,
        )
    except Exception as e:
        conn.rollback()
        log.error(f"No pude guardar resultado del Auditor: {e}")
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(description="Agente Auditor de la Capa 3 (cruzado)")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--detective-modelo", choices=["groq", "gemini", "claude"], default=None)
    args = parser.parse_args()

    conn = conectar_db()
    try:
        resultados = ejecutar_auditor(conn, args.ticker, args.detective_modelo)
        for resultado in resultados:
            guardar_resultado_auditor(conn, resultado)
            print(json.dumps(resultado["respuesta_llm"], indent=2, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

