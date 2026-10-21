"""
experimento_auditor.py — ¿el Auditor de Claude es riguroso o es duro por sistema?

Con el protocolo 2, el Auditor de Claude (que revisa a Gemini) empeoró el
veredicto en las 25 auditorías, y el de Gemini (que revisa a Claude) solo en
12 de 25. Como los Detectives reparten sus veredictos igual, hay dos
explicaciones: que los análisis de Gemini sean peores, o que Claude, como
auditor, empeore casi todo lo que le llega.

Para separarlas, hago que el mismo auditor revise los análisis de los DOS
Detectives con el mismo prompt:
  - Claude audita a Claude: si también empeora casi todo, es el auditor.
  - Claude audita a Gemini ya está en la base (las 25 de producción).
Si Claude empeora de forma parecida sus propios análisis y los de Gemini, el
sesgo es del auditor y no dice nada sobre la calidad de cada Detective.

No guarda nada en `auditorias` (no quiero mezclar un experimento con las
estadísticas de producción): escribe un CSV y lo resume por pantalla. Las
llamadas sí se registran en llm_calls, porque cuestan dinero de verdad
(~2 céntimos por empresa).

Cómo usarlo:
  python experimento_auditor.py --limite 3     # prueba corta
  python experimento_auditor.py                # las 25 candidatas
"""

import csv
import time
import json
import logging
import argparse
from collections import Counter

import detective
import auditor

log = logging.getLogger(__name__)

SALIDA = "resultados_experimento_auditor.csv"


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
    prompt = auditor.construir_prompt_auditor(contexto, respuesta_detective)
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
