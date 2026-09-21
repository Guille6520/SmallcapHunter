"""
grafo_capa3.py — la Capa 3 (Detective + Auditor) orquestada como un
grafo de LangGraph, en vez de tres comandos manuales encadenados a mano.

Por qué un grafo y no solo tres funciones en secuencia: los dos
Detectives (Groq y el modelo secundario) son votos ciegos e
independientes por diseño — nunca se enseñan el uno al otro (ver
detective.py). Eso es, literalmente, un fan-out: dos ramas que arrancan
del mismo estado inicial y corren en paralelo. El Auditor solo tiene
sentido cuando AMBAS ramas han terminado — un fan-in. LangGraph modela
esto de forma explícita (nodos, aristas, estado compartido) en vez de
dejarlo implícito en el orden de las líneas de un script.

No reimplemento el análisis: cada nodo del grafo llama directamente a
las funciones ya probadas de detective.py y auditor.py (ejecutar_detective,
guardar_resultado, ejecutar_auditor, guardar_resultado_auditor). El
grafo es la capa de orquestación, no una reescritura del agente.

Quién usa este módulo:
  - orquestador.py lo invoca como subproceso (un ticker, uno o los dos
    modelos) en vez de encadenar `detective.py` + `detective.py` +
    `auditor.py` por separado — así la pasada automática de 48h también
    corre sobre el grafo, no es una demo aislada.
  - api.py lo invoca en proceso (import directo) desde el endpoint
    POST /analizar/{ticker}.
  - Uso manual desde la terminal, para un ticker suelto:
      python grafo_capa3.py --ticker XXXX
      python grafo_capa3.py --ticker XXXX --modelos groq
"""

import json
import logging
import argparse
from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, START, END

import detective
import auditor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class EstadoCapa3(TypedDict):
    ticker: str
    conn: Any                      # conexión psycopg2 ya abierta, no serializada
    modelos_a_ejecutar: list       # subconjunto de ["groq", modelo_secundario()]
    resultado_groq: Optional[dict]
    resultado_secundario: Optional[dict]
    fallo_groq: bool
    fallo_secundario: bool
    auditorias_guardadas: list


def _nodo_detective(modelo: str, campo_resultado: str, campo_fallo: str):
    """
    Fábrica de nodos: devuelvo una función de nodo cerrada sobre qué
    modelo le toca correr y en qué claves del estado escribe. Groq y el
    modelo secundario comparten la misma lógica de nodo — solo cambia
    el modelo — así que no duplico el cuerpo por cada uno.

    Cada nodo abre su PROPIA conexión a la base de datos en vez de usar
    state["conn"]. LangGraph ejecuta los dos nodos Detective del mismo
    superstep en paralelo (fan-out real desde START), y una conexión
    psycopg2 es una única transacción: si compartieran la conexión, el
    rollback de un nodo tras un fallo (p.ej. rate-limit del LLM) podía
    deshacer el INSERT todavía no confirmado del otro nodo, que además
    ya había mandado el aviso de Telegram como si se hubiera guardado.
    Con una conexión por nodo, cada transacción queda aislada de verdad.
    """
    def nodo(state: EstadoCapa3) -> dict:
        if modelo not in state["modelos_a_ejecutar"]:
            return {}

        conn = detective.conectar_db()
        try:
            try:
                resultado = detective.ejecutar_detective(conn, state["ticker"], modelo)
            except Exception as e:
                log.error(f"Detective ({modelo}) sobre {state['ticker']}: excepción — {e}")
                return {campo_fallo: True}

            if not resultado:
                return {campo_fallo: True}

            detective.guardar_resultado(
                conn, resultado["contexto"]["empresa_id"], modelo, resultado
            )
            return {campo_resultado: resultado}
        finally:
            conn.close()

    return nodo


def nodo_auditor(state: EstadoCapa3) -> dict:
    """
    Fan-in: LangGraph no ejecuta este nodo hasta que las dos ramas de
    Detective (las que tienen una arista hacia él) terminan su superstep
    — no necesito esperar nada a mano aquí, es la semántica del grafo.

    Si los dos Detectives fallaron, no hay nada que auditar — reutilizo
    ejecutar_auditor tal cual, que ya sabe saltar lo que no tenga
    análisis previo guardado.
    """
    if state.get("fallo_groq") and state.get("fallo_secundario"):
        log.warning(f"{state['ticker']}: los dos Detectives fallaron — no hay nada que auditar")
        return {"auditorias_guardadas": []}

    try:
        resultados = auditor.ejecutar_auditor(state["conn"], state["ticker"])
    except Exception as e:
        log.error(f"Auditor sobre {state['ticker']}: excepción — {e}")
        return {"auditorias_guardadas": []}

    for r in resultados:
        auditor.guardar_resultado_auditor(state["conn"], r)

    return {"auditorias_guardadas": resultados}


def construir_grafo():
    """
    START
      ├─> detective_groq        ─┐
      └─> detective_secundario  ─┴─> auditor ─> END

    Los dos nodos Detective arrancan del mismo superstep (fan-out desde
    START); auditor los espera a los dos antes de correr (fan-in). Cada
    nodo respeta modelos_a_ejecutar y no hace nada si su modelo no está
    en la lista — así el mismo grafo sirve tanto para "los dos modelos"
    como para "reintentar solo uno" sin tener dos grafos distintos.
    """
    grafo = StateGraph(EstadoCapa3)

    grafo.add_node("detective_groq", _nodo_detective("groq", "resultado_groq", "fallo_groq"))
    grafo.add_node(
        "detective_secundario",
        _nodo_detective(detective.modelo_secundario(), "resultado_secundario", "fallo_secundario"),
    )
    grafo.add_node("auditor", nodo_auditor)

    grafo.add_edge(START, "detective_groq")
    grafo.add_edge(START, "detective_secundario")
    grafo.add_edge("detective_groq", "auditor")
    grafo.add_edge("detective_secundario", "auditor")
    grafo.add_edge("auditor", END)

    return grafo.compile()


def ejecutar_grafo_capa3(conn, ticker: str, modelos_a_ejecutar: list = None) -> dict:
    """
    Punto de entrada reutilizable (lo usan api.py y orquestador.py, y
    main() de aquí abajo). Devuelve el estado final del grafo: qué
    Detective(es) tuvieron éxito o fallaron, y qué auditorías se
    guardaron — con eso el caller decide qué hacer (marcar un modelo
    como sin cuota, notificar, etc.) sin tener que abrir el grafo.
    """
    if modelos_a_ejecutar is None:
        modelos_a_ejecutar = ["groq", detective.modelo_secundario()]

    app = construir_grafo()
    estado_inicial: EstadoCapa3 = {
        "ticker": ticker,
        "conn": conn,
        "modelos_a_ejecutar": modelos_a_ejecutar,
        "resultado_groq": None,
        "resultado_secundario": None,
        "fallo_groq": False,
        "fallo_secundario": False,
        "auditorias_guardadas": [],
    }
    return app.invoke(estado_inicial)


def main():
    parser = argparse.ArgumentParser(
        description="Capa 3 completa (Detective x2 + Auditor cruzado) como grafo de LangGraph"
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument(
        "--modelos", default=None,
        help="Coma-separado: groq,gemini. Por defecto: groq + el modelo secundario configurado.",
    )
    args = parser.parse_args()

    modelos = args.modelos.split(",") if args.modelos else None

    conn = detective.conectar_db()
    try:
        estado_final = ejecutar_grafo_capa3(conn, args.ticker, modelos)
    finally:
        conn.close()

    resumen = {
        "ticker": args.ticker,
        "detective_groq_ok": estado_final.get("resultado_groq") is not None,
        "detective_secundario_ok": estado_final.get("resultado_secundario") is not None,
        "fallo_groq": estado_final.get("fallo_groq", False),
        "fallo_secundario": estado_final.get("fallo_secundario", False),
        "auditorias_guardadas": len(estado_final.get("auditorias_guardadas") or []),
    }
    log.info(f"Resumen {args.ticker}: {resumen}")
    # Línea final estable para que un caller (orquestador.py) pueda
    # capturar stdout y parsear el resultado sin depender del formato
    # de los logs de arriba, que son para lectura humana.
    print("RESULTADO_JSON: " + json.dumps(resumen, ensure_ascii=False))


if __name__ == "__main__":
    main()
