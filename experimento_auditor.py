"""
experimento_auditor.py — ¿el Auditor de Claude es riguroso o es duro por sistema?

SCRIPT DE INVESTIGACIÓN, NO FORMA PARTE DEL PIPELINE. Lo conservo para que el
experimento sea reproducible; ni el orquestador ni el dashboard lo ejecutan. El
Auditor que sí corre hoy es auditor.py (que verifica y da un sello, no un
veredicto); el prompt de este archivo es el de la v1, ya retirado.

Con el protocolo 2, el Auditor v1 de Claude (que revisaba a Gemini) declaró EMPEORA
en todas sus auditorías (31 de 31 en la base hoy), y el de Gemini (que revisaba a
Claude) en 16 de 31. Ojo: es lo que declaraban, no lo que ocurría; en el conjunto de
las 130 auditorías v1 declaró EMPEORA en 74 % (96 de 130) y su veredicto real fue más
bajo en 45 %. Como los Detectives reparten sus veredictos igual, hay dos
explicaciones: que los análisis de Gemini sean peores, o que Claude, como auditor,
declare que empeora casi todo lo que le llega.

Para separarlas, hago que el mismo auditor revise los análisis de los DOS
Detectives con el mismo prompt:
  - Claude audita a Claude: si también empeora casi todo, es el auditor.
  - Claude audita a Gemini ya está en la base (las auditorías de la v1).
Si Claude empeora de forma parecida sus propios análisis y los de Gemini, el
sesgo es del auditor y no dice nada sobre la calidad de cada Detective.

Resultado (resultados_experimento_auditor.csv): auditando 24 análisis de sí mismo,
Claude declaró EMPEORA en 24 de 24 (su veredicto real fue más bajo en 16 e igual en 8).
No era el modelo, era la tarea: un LLM no da un juicio global estable sobre otro
análisis, y por eso el Auditor actual no opina.

No guarda nada en `auditorias` (no quiero mezclar un experimento con las
estadísticas de producción): escribe un CSV y lo resume por pantalla. Las
llamadas sí se registran en llm_calls, porque cuestan dinero de verdad
(~2 céntimos por empresa).

Cómo usarlo:
  python experimento_auditor.py --limite 3     # prueba corta
  python experimento_auditor.py                # todas las que tienen análisis del protocolo vigente
"""

import csv
import time
import json
import logging
import argparse
from collections import Counter

import detective
from detective import MAX_CARACTERES_MDA, formatear_eventos_8k, formatear_senales_mercado

log = logging.getLogger(__name__)

SALIDA = "resultados_experimento_auditor.csv"


def construir_prompt_auditor(contexto: dict, respuesta_detective: dict) -> str:
    """El prompt del Auditor de la v1, RETIRADO del pipeline y conservado aquí solo para
    que este experimento siga siendo reproducible. Pedía un veredicto global sobre el
    análisis del Detective, y ese veredicto era ruido: declaró EMPEORA en 74 % de los
    casos (96 de 130) y su veredicto real fue más bajo en 45 %, también con sus propios análisis, y añadirle la tarea de buscar fortalezas
    infravaloradas no lo arregló. El Auditor actual (auditor.py) no da veredicto."""
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
4. CITAS: si afirmas algo nuevo que el Detective no dijo (sea riesgo o fortaleza), cita (máx 20 palabras) el texto que lo respalda. En "cita_literal" copia el texto EXACTO, carácter por carácter, en el idioma original del documento (inglés): NO lo traduzcas, no uses "..." ni combines fragmentos de sitios distintos, y no añadas referencias, fuentes ni corchetes. En "cita_es" pon la traducción al español de esa misma cita. Si no puedes citarlo, no lo afirmes.
5. Da tu propio veredicto de interés, independiente del preliminar (NO es consejo de inversión): MUY_INTERESANTE, INTERESANTE, o NADA_INTERESANTE
6. Indica explícitamente si tu veredicto CONFIRMA, MEJORA o EMPEORA el veredicto preliminar del Detective, y por qué

Responde ÚNICAMENTE con un JSON válido, sin texto antes ni después, redactado en español salvo el campo "cita_literal" (que va en el idioma original del documento), con esta estructura exacta:
{{
  "tesis_se_sostiene": true o false,
  "riesgos_omitidos_por_detective": ["string"],
  "fortalezas_omitidas_por_detective": ["string"],
  "citas_nuevas": [
    {{"afirmacion": "string en español", "cita_literal": "texto EXACTO del documento, sin traducir", "cita_es": "traducción al español de la cita"}}
  ],
  "veredicto_auditor": "MUY_INTERESANTE|INTERESANTE|NADA_INTERESANTE",
  "relacion_con_veredicto_detective": "CONFIRMA|MEJORA|EMPEORA",
  "justificacion": "string breve explicando el cambio o confirmación"
}}"""


def ultimos_analisis(conn, modelo_detective: str) -> list:
    """El último análisis del protocolo vigente de un Detective, por empresa."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select distinct on (a.empresa_id) a.id, e.ticker, a.respuesta_llm
            from auditorias a join empresas e on e.id = a.empresa_id
            where a.modelo_llm = %s
              and a.respuesta_llm ? 'veredicto_preliminar'
              and (a.respuesta_llm->>'version_protocolo')::int >= %s
            order by a.empresa_id, a.fecha_analisis desc
            """,
            (modelo_detective, detective.VERSION_PROTOCOLO),
        )
        return cur.fetchall()
    finally:
        cur.close()


def auditar(conn, ticker: str, respuesta_detective: dict, modelo_auditor: str):
    contexto = detective.obtener_contexto_empresa(conn, ticker)
    prompt = construir_prompt_auditor(contexto, respuesta_detective)
    t0 = time.time()
    try:
        texto = detective.llamar_modelo(modelo_auditor, prompt)
    except Exception as e:
        detective.registrar_llamada_llm(
            origen="auditor", modelo=modelo_auditor, ticker=ticker,
            segundos=time.time() - t0, exito=False, tipo_error=detective.describir_error(e),
        )
        return None
    try:
        resp = detective.parsear_json_llm(texto)
    except json.JSONDecodeError as e:
        detective.registrar_llamada_llm(
            origen="auditor", modelo=modelo_auditor, ticker=ticker,
            tokens_entrada=getattr(texto, "tokens_entrada", None),
            tokens_salida=getattr(texto, "tokens_salida", None),
            segundos=time.time() - t0, exito=False, tipo_error=detective.describir_error_json(texto, e),
        )
        return None
    detective.registrar_llamada_llm(
        origen="auditor", modelo=modelo_auditor, ticker=ticker,
        tokens_entrada=getattr(texto, "tokens_entrada", None),
        tokens_salida=getattr(texto, "tokens_salida", None),
        segundos=time.time() - t0, exito=True,
    )
    return resp


def main():
    parser = argparse.ArgumentParser(description="¿Claude empeora lo que audita por sistema?")
    parser.add_argument("--detective", default="claude", choices=["claude", "gemini"],
                        help="de qué Detective son los análisis que se auditan")
    parser.add_argument("--auditor", default="claude", choices=["claude", "gemini"])
    parser.add_argument("--limite", type=int, default=None, help="cuántas empresas (para probar)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

    conn = detective.conectar_db()
    try:
        analisis = ultimos_analisis(conn, args.detective)
        if args.limite:
            analisis = analisis[:args.limite]
        filas = []
        for auditoria_id, ticker, respuesta in analisis:
            log.info(f"{args.auditor} audita a {args.detective} sobre {ticker}...")
            r = auditar(conn, ticker, respuesta, args.auditor)
            if r is None:
                continue
            filas.append({
                "ticker": ticker, "detective": args.detective, "auditor": args.auditor,
                "veredicto_detective": respuesta.get("veredicto_preliminar"),
                "veredicto_auditor": r.get("veredicto_auditor"),
                "relacion": r.get("relacion_con_veredicto_detective"),
                "tesis_se_sostiene": r.get("tesis_se_sostiene"),
            })
            time.sleep(1)
    finally:
        conn.close()

    if not filas:
        print("Sin resultados.")
        return
    with open(SALIDA, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)

    relaciones = Counter(f["relacion"] for f in filas)
    print(f"\n{args.auditor} audita a {args.detective}: {len(filas)} empresas -> {SALIDA}")
    print("Relación con el veredicto del Detective:", dict(relaciones))
    print("Veredicto del Detective -> del Auditor:", dict(Counter(
        f"{f['veredicto_detective']} -> {f['veredicto_auditor']}" for f in filas)))


if __name__ == "__main__":
    main()
