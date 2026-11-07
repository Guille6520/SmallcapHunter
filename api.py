"""
api.py — interfaz HTTP de SmallCap Hunter con FastAPI.

No sustituye al dashboard de Streamlit (dashboard.py) — es una segunda
interfaz sobre los MISMOS datos, pensada para consumo por programa (un
frontend propio, un script externo, un cron con curl) en vez de por un
humano frente a un navegador. Reutilizo la misma capa de acceso a datos
que ya usan detective.py y dashboard.py (obtener_contexto_empresa,
DB_CONFIG) en vez de duplicar consultas SQL.

Endpoints:
  GET  /health                       — vivo/muerto, para healthchecks
  GET  /candidatas                   — ranking de Capa 2 (igual que el
                                        panorama del dashboard)
  GET  /candidatas/{ticker}          — ficha completa de una empresa
                                        (scores, insiders, MD&A, veredictos)
  GET  /candidatas/{ticker}/similares — casos históricos por RAG (Capa 4)
  POST /candidatas/{ticker}/analizar  — dispara la Capa 3 completa
                                        (grafo de LangGraph) para este ticker

Cómo levantarla:
  uvicorn api:app --reload --port 8000

Documentación interactiva automática (Swagger) una vez levantada:
  http://127.0.0.1:8000/docs
"""

import os
import json
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from detective import (
    obtener_contexto_empresa, buscar_casos_similares_rag,
    formatear_casos_similares_rag,
)
from grafo_capa3 import ejecutar_grafo_capa3
from verificaciones import VERSION_AUDITOR, asegurar_esquema as asegurar_esquema_verificaciones

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Los dos votos ciegos que existen. Lo que llegue por la API se valida contra esto.
MODELOS_PERMITIDOS = ("claude", "gemini")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

app = FastAPI(
    title="SmallCap Hunter API",
    description="Criba de small caps con señales tempranas de mejora, sobre datos públicos de la SEC.",
    version="1.0.0",
)


@contextmanager
def conexion():
    """
    Una conexión por request (autocommit, pensada para consultas de lectura). No reutilizo un pool
    global a propósito: es una API de bajo tráfico (uso personal /
    portafolio, no un producto con miles de usuarios concurrentes), así
    que la simplicidad de abrir y cerrar por request vale más que la
    complejidad de gestionar un pool para una carga que nunca la va a
    necesitar.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


class CandidataResumen(BaseModel):
    ticker: str
    nombre: str
    sector: Optional[str] = None
    market_cap_usd: Optional[int] = None
    score_total: Optional[int] = None
    veredicto: Optional[str] = None


class CasoSimilar(BaseModel):
    ticker: str
    nombre: str
    anio_fiscal: int
    trimestre: int
    similitud_pct: float
    extracto: str


class AnalisisResumen(BaseModel):
    ticker: str
    detective_claude_ok: bool
    detective_gemini_ok: bool
    auditorias_guardadas: int


@app.get("/health")
def health():
    """
    Comprueba también la conexión a la BD, no solo que el proceso esté
    vivo — un healthcheck que solo dice "sí, el proceso de Python
    existe" no sirve para detectar el caso real (Postgres caído).
    """
    try:
        with conexion() as conn:
            cur = conn.cursor()
            cur.execute("select 1")
            cur.close()
        return {"status": "ok", "db": "ok"}
    except psycopg2.Error as e:
        # El detalle (host, usuario, puerto) se queda en el log del servidor: no
        # se lo doy a quien consulta el endpoint.
        log.error(f"/health: la base de datos no responde: {e}")
        raise HTTPException(status_code=503, detail="BD no disponible")


@app.get("/candidatas", response_model=list[CandidataResumen])
def listar_candidatas(limite: int = 50, solo_analizadas: bool = False):
    """
    Ranking de candidatas. Por defecto, el mismo ranking de Capa 2 que
    muestra el panorama del dashboard (score_total desc, sin veredicto
    LLM todavía). Con solo_analizadas=true, en cambio, devuelvo las que
    YA pasaron por algún Detective, con el veredicto más reciente de un
    Detective (no el de una auditoría de la v1, que daba un veredicto propio
    y era ruido: ver el README). El sello del Auditor v2 de cada análisis
    (tabla verificaciones) lo da la ficha, en /candidatas/{ticker}.
    """
    limite = max(1, min(limite, 200))

    with conexion() as conn:
        cur = conn.cursor()
        try:
            if solo_analizadas:
                # El veredicto más reciente de un Detective por empresa (solo filas con
                # veredicto_preliminar: las del Auditor v1 y las de score quedan fuera), con
                # su score de Capa 2 si lo tiene (una empresa que ya no pasa Capa 1 sale
                # sin score, no con un 0 inventado), las de mejor score primero.
                cur.execute(
                    """
                    with ultimo as (
                        select distinct on (a.empresa_id) a.empresa_id, a.veredicto
                        from auditorias a
                        where a.veredicto is not null
                          and a.respuesta_llm ? 'veredicto_preliminar'
                        order by a.empresa_id, a.fecha_analisis desc
                    )
                    select e.ticker, e.nombre, e.sector, e.market_cap_usd,
                           s.score_total, u.veredicto
                    from ultimo u
                    join empresas e on e.id = u.empresa_id
                    left join lateral (
                        select score_total from auditorias
                        where empresa_id = e.id and veredicto is null
                        order by fecha_analisis desc limit 1
                    ) s on true
                    order by s.score_total desc nulls last, e.ticker
                    limit %s
                    """,
                    (limite,),
                )
            else:
                cur.execute(
                    """
                    select e.ticker, e.nombre, e.sector, e.market_cap_usd,
                           a.score_total, null as veredicto
                    from auditorias a
                    join empresas e on e.id = a.empresa_id
                    where a.veredicto is null
                    order by a.score_total desc
                    limit %s
                    """,
                    (limite,),
                )
            filas = cur.fetchall()
        finally:
            cur.close()

    columnas = ["ticker", "nombre", "sector", "market_cap_usd", "score_total", "veredicto"]
    return [CandidataResumen(**dict(zip(columnas, fila))) for fila in filas]


@app.get("/candidatas/{ticker}")
def ficha_candidata(ticker: str):
    """
    La misma ficha que ve el Detective antes de analizar: scores,
    insiders, MD&A, eventos 8-K, señales de mercado — más los
    veredictos ya guardados en auditorias, que obtener_contexto_empresa
    no trae (esa función es el contexto de ENTRADA al LLM, no el
    historial de análisis ya hechos).
    """
    with conexion() as conn:
        contexto = obtener_contexto_empresa(conn, ticker.upper())
        if not contexto:
            raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")

        asegurar_esquema_verificaciones(conn)
        cur = conn.cursor()
        try:
            # Solo análisis de Detective (los que llevan veredicto_preliminar; las auditorías
            # de la v1 daban un veredicto propio que era ruido y no se mezclan), cada uno con
            # el sello que le puso el Auditor v2, si ya lo verificó.
            cur.execute(
                """
                select a.modelo_llm, a.veredicto, a.respuesta_llm, a.verificacion_citas, a.fecha_analisis,
                       v.sello, v.modelo_auditor
                from auditorias a
                left join lateral (
                    select sello, modelo_auditor from verificaciones
                    where detective_auditoria_id = a.id and version_auditor = %s
                    order by fecha desc limit 1
                ) v on true
                where a.empresa_id = %s and a.veredicto is not null
                  and a.respuesta_llm ? 'veredicto_preliminar'
                order by a.fecha_analisis desc
                limit 10
                """,
                (VERSION_AUDITOR, contexto["empresa_id"]),
            )
            analisis = cur.fetchall()
        finally:
            cur.close()

    return {
        "ticker": contexto["ticker"],
        "nombre": contexto["nombre"],
        "sector": contexto["sector"],
        "market_cap_usd": contexto["market_cap"],
        "bolsa": contexto["bolsa"],
        "scores": contexto["scores"],
        "texto_mda_disponible": bool(contexto["texto_mda"]),
        "analisis": [
            {
                "modelo": modelo, "veredicto": veredicto,
                "respuesta": respuesta, "verificacion_citas": verificacion,
                "fecha": fecha.isoformat() if fecha else None,
                # Sello del Auditor sobre este análisis (VERIFICADO / CON_AVISOS) o null si aún no se verificó
                "sello": sello, "sello_puesto_por": modelo_auditor,
            }
            for modelo, veredicto, respuesta, verificacion, fecha, sello, modelo_auditor in analisis
        ],
    }


@app.get("/candidatas/{ticker}/similares", response_model=list[CasoSimilar])
def casos_similares(ticker: str, k: int = 3):
    """
    Expone directamente la Capa 4 (RAG): qué trimestres históricos de
    OTRAS empresas se parecen semánticamente al MD&A más reciente de
    esta candidata. Útil para depurar o demostrar el RAG sin tener que
    lanzar un análisis completo de Capa 3.
    """
    k = max(1, min(k, 10))

    with conexion() as conn:
        contexto = obtener_contexto_empresa(conn, ticker.upper())
        if not contexto:
            raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")
        if not contexto["texto_mda"]:
            raise HTTPException(
                status_code=422,
                detail=f"{ticker} no tiene texto_mda todavía — ejecuta ingesta_10q.py",
            )

        casos = buscar_casos_similares_rag(conn, contexto["empresa_id"], contexto["texto_mda"], k)

    return [
        CasoSimilar(
            ticker=c["ticker"], nombre=c["nombre"],
            anio_fiscal=c["anio_fiscal"], trimestre=c["trimestre"],
            similitud_pct=round((1 - c["distancia"]) * 100, 1),
            extracto=(c["texto_mda"] or "")[:500],
        )
        for c in casos
    ]


@app.post("/candidatas/{ticker}/analizar", response_model=AnalisisResumen)
def analizar_candidata(ticker: str, modelos: Optional[str] = None):
    """
    Dispara la Capa 3 completa (Detective x2 + Auditor cruzado) para
    este ticker, en proceso — invoca ejecutar_grafo_capa3 directamente,
    no como subproceso (eso es lo que hace orquestador.py, que sí
    necesita aislamiento de proceso; una request HTTP ya está aislada
    por naturaleza).

    Es una llamada SÍNCRONA y tarda del orden de 1-2 minutos (según llm_calls:
    cada Detective ronda los 35 s, en paralelo, y después el Auditor hace dos
    verificaciones de 25-40 s cada una; más si hay reintentos) — para un volumen alto de peticiones esto
    debería ser una tarea en background con polling, pero para el uso
    real del proyecto (unas pocas empresas al día) no compensa la
    complejidad de una cola de tareas.
    """
    ticker = ticker.upper()
    lista_modelos = [m.strip() for m in modelos.split(",") if m.strip()] if modelos else ["claude", "gemini"]
    # Solo los dos modelos que existen: un nombre cualquiera iría a llamar_modelo, que
    # lanza un error, y cada petición con un nombre inventado costaría una conexión
    # a la base de datos y una ejecución del grafo.
    desconocidos = [m for m in lista_modelos if m not in MODELOS_PERMITIDOS]
    if desconocidos or not lista_modelos:
        raise HTTPException(
            status_code=400,
            detail=f"modelos no válidos: {desconocidos or modelos!r}. Usa claude, gemini o ambos separados por coma.",
        )

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        contexto = obtener_contexto_empresa(conn, ticker)
        if not contexto:
            raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")
        if not contexto["texto_mda"]:
            raise HTTPException(
                status_code=422,
                detail=f"{ticker} no tiene texto_mda todavía — ejecuta ingesta_10q.py",
            )

        estado_final = ejecutar_grafo_capa3(conn, ticker, lista_modelos)
    finally:
        conn.close()

    return AnalisisResumen(
        ticker=ticker,
        detective_claude_ok=estado_final.get("resultado_claude") is not None,
        detective_gemini_ok=estado_final.get("resultado_gemini") is not None,
        auditorias_guardadas=len(estado_final.get("auditorias_guardadas") or []),
    )
