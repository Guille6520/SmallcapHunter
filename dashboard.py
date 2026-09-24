"""
dashboard.py — interfaz Streamlit de SmallCap Hunter

Tres vistas:
  - Panorama: el embudo completo (de miles de empresas a un puñado de
    candidatas) y los últimos veredictos de los agentes.
  - Empresa: la ficha de una candidata — scores desagregados, compras
    de insiders, evolución de revenue y margen, el MD&A real, y lo que
    dijeron el Detective y el Auditor.
  - Chat: conversación libre con un LLM que tiene delante el MISMO
    contexto que vio el Detective (scores, insiders, MD&A, veredictos).
    Para preguntarle cosas como "¿por qué el score temporal es tan
    bajo?" o "resúmeme los riesgos en dos frases".

El chat reutiliza obtener_contexto_empresa de detective.py — así lo que
el chat "sabe" de una empresa es exactamente lo que sabe el pipeline,
no una versión distinta. Nota honesta: el chat NO pasa por el
verificador de citas (es conversación, no análisis persistido), así que
sus respuestas pueden contener imprecisiones — el veredicto oficial es
el de la tabla auditorias, no lo que diga el chat.

Cómo lanzarlo:
  streamlit run dashboard.py
"""

import os
import json
import logging

import pandas as pd
import psycopg2
import streamlit as st

from detective import obtener_contexto_empresa, MAX_CARACTERES_MDA
from notificador_telegram import enviar_telegram
from dotenv import load_dotenv

# Cargo el .env de la carpeta si existe — así las claves no dependen de
# pegarlas a mano en cada sesión nueva de PowerShell.
load_dotenv()

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

st.set_page_config(
    page_title="SmallCap Hunter",
    page_icon="🦄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Un poco de CSS para que no parezca el Streamlit por defecto de todos
# los tutoriales: tarjetas con borde suave, cabecera con degradado, y
# los veredictos con su color de semáforo.
st.markdown("""
<style>
    .cabecera {
        background: linear-gradient(90deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%);
        padding: 1.2rem 1.6rem;
        border-radius: 14px;
        margin-bottom: 1rem;
    }
    .cabecera h1 { color: #e94560; margin: 0; font-size: 1.9rem; }
    .cabecera p  { color: #cfd8e3; margin: 0.3rem 0 0 0; font-size: 0.95rem; }
    [data-testid="stMetric"] {
        background: rgba(151, 166, 195, 0.08);
        border: 1px solid rgba(151, 166, 195, 0.25);
        border-radius: 12px;
        padding: 0.8rem;
    }
    .veredicto-MUY_INTERESANTE  { color: #21bf73; font-weight: 700; }
    .veredicto-INTERESANTE      { color: #f0a500; font-weight: 700; }
    .veredicto-NADA_INTERESANTE { color: #e94560; font-weight: 700; }
    .veredicto-ALUCINACION      { color: #9b59b6; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


# ---------- acceso a datos ----------

@st.cache_resource
def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    # autocommit: sobre todo lecturas, con una excepción -- el cacheo de
    # resumen_negocio en obtener_resumen_negocio() sí escribe. Es una
    # sola sentencia de bajo riesgo, no necesita transacción explícita.
    conn.autocommit = True
    return conn


@st.cache_data(ttl=120)
def query_df(sql: str, params: tuple = None) -> pd.DataFrame:
    """Query -> DataFrame, cacheada 2 minutos para no machacar la BD
    con cada rerun de Streamlit (que re-ejecuta el script entero)."""
    conn = conectar_db()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        columnas = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columnas)
    finally:
        cur.close()


def leer_corte(conn=None) -> int:
    df = query_df("select valor from configuracion where clave = 'score_minimo_llm'")
    return int(df.iloc[0]["valor"]) if not df.empty else 25


# ---------- llamadas LLM para el chat (texto libre, sin JSON) ----------

def chat_groq(mensajes: list) -> str:
    # "groq" es el nombre del hueco, no el proveedor real -- ver
    # detective.llamar_groq() para el porqué del cambio a OpenRouter.
    # max_tokens + reasoning.effort bajo: mismo motivo que en
    # detective.llamar_groq() -- sin esto, Nemotron puede gastarse el
    # presupuesto entero razonando y devolver una respuesta vacía.
    import requests
    respuesta = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": "nvidia/nemotron-3-super-120b-a12b:free",
            "messages": mensajes,
            "temperature": 0.4,
            "max_tokens": 4000,
            "reasoning": {"effort": "low"},
        },
        timeout=90,
    )
    respuesta.raise_for_status()
    data = respuesta.json()
    if "choices" not in data:
        raise RuntimeError(f"OpenRouter no devolvió 'choices': {data}")
    return data["choices"][0]["message"]["content"]


def chat_gemini(mensajes: list) -> str:
    # Gemini no usa el formato de roles de OpenAI — aplano la
    # conversación en un solo texto. Para un chat de consulta sobre
    # una empresa es más que suficiente.
    from google import genai
    cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
    plano = "\n\n".join(
        f"[{m['role'].upper()}]\n{m['content']}" for m in mensajes
    )
    respuesta = cliente.models.generate_content(
        model="gemini-2.5-flash",
        contents=plano + "\n\n[ASSISTANT]\n",
    )
    return respuesta.text


def construir_contexto_chat(conn, ticker: str) -> str:
    """
    El system prompt del chat: el mismo contexto que ve el Detective,
    más los veredictos ya emitidos. Con instrucción explícita de
    ceñirse a estos datos — el chat no tiene verificador de citas
    detrás, así que al menos le acoto la materia prima.
    """
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        return None

    sp, sc, st_, scat, stotal = contexto["scores"] or (0, 0, 0, 0, 0)

    insiders = "\n".join(
        f"- {n} ({c or 'cargo no especificado'}): ${imp:,.0f} el {f}"
        for n, c, f, imp in contexto["transacciones"] if imp is not None
    ) or "(sin detalle de importes)"

    veredictos = query_df(
        """
        select a.modelo_llm, a.veredicto, a.fecha_analisis, a.respuesta_llm
        from auditorias a join empresas e on e.id = a.empresa_id
        where e.ticker = %s and a.veredicto is not null
        order by a.fecha_analisis desc limit 6
        """,
        (ticker,)
    )
    resumen_veredictos = "\n".join(
        f"- {r.modelo_llm} ({r.fecha_analisis:%Y-%m-%d}): {r.veredicto}"
        for r in veredictos.itertuples()
    ) or "(todavía sin análisis LLM)"

    mda = (contexto["texto_mda"] or "(sin MD&A descargado)")[:MAX_CARACTERES_MDA]

    return f"""Eres el analista de SmallCap Hunter, un sistema que busca small caps en fase pre-explosiva. El usuario va a conversar contigo sobre UNA empresa concreta. Responde en español, breve y directo, y básate SOLO en los datos de abajo. Si algo no está en estos datos, di que no lo tienes — no lo inventes.

EMPRESA: {contexto['nombre']} ({ticker}) | Sector: {contexto['sector']} | Bolsa: {contexto['bolsa']} | Market cap: ${(contexto['market_cap'] or 0):,.0f}

SCORES CAPA 2 (0-10 cada uno): precio 52w={sp}, convicción insiders={sc}, aceleración={st_}, catalizador={scat}, TOTAL={stotal}/40

COMPRAS DE INSIDERS RECIENTES:
{insiders}

VEREDICTOS DE LOS AGENTES:
{resumen_veredictos}

MD&A DEL ÚLTIMO 10-Q (puede estar recortado):
\"\"\"{mda}\"\"\""""


def obtener_resumen_negocio(conn, empresa_id: int, contexto: dict) -> str:
    """Resumen corto de a qué se dedica la empresa, en lenguaje llano.
    No es parte del análisis de inversión (no pasa por el Detective ni
    el verificador de citas) -- se genera una vez y se cachea en
    empresas.resumen_negocio.

    Pruebo Groq/OpenRouter primero y caigo a Gemini si falla -- al
    revés de lo esperado a propósito: la cuota de Gemini (20/día) la
    comparte el pipeline real (Detective/Auditor), y no quiero que
    esta función, que es un extra cosmético, se la coma antes de que
    la necesite un análisis de verdad. La cuota de OpenRouter (50/día,
    por petición) es más barata de gastar aquí.
    """
    cur = conn.cursor()
    cur.execute("select resumen_negocio from empresas where id = %s", (empresa_id,))
    existente = cur.fetchone()[0]
    if existente:
        cur.close()
        return existente

    prompt = f"""Explica en 2-3 frases, en español y sin jerga financiera, a qué se dedica esta empresa: qué vende, a quién, y en qué sector opera. Básate solo en esto:

EMPRESA: {contexto['nombre']} ({contexto['ticker']})
Sector: {contexto['sector']}

TEXTO MD&A:
\"\"\"{(contexto['texto_mda'] or '')[:4000]}\"\"\"

No hables de resultados financieros ni de si es buena inversión, solo de qué hace la empresa."""

    mensajes = [{"role": "user", "content": prompt}]
    resumen, errores = None, []
    for nombre, llamar in [("groq", chat_groq), ("gemini", chat_gemini)]:
        try:
            resumen = llamar(mensajes)
            break
        except Exception as e:
            errores.append(f"{nombre}: {str(e)[:150]}")

    if resumen is None:
        cur.close()
        return "(no se pudo generar -- " + " | ".join(errores) + ")"

    cur.execute("update empresas set resumen_negocio = %s where id = %s", (resumen, empresa_id))
    cur.close()
    return resumen


# ---------- componentes de la interfaz ----------

def cabecera():
    st.markdown(
        '<div class="cabecera"><h1>🦄 SmallCap Hunter</h1>'
        '<p>Detección de small caps en fase pre-explosiva — '
        'insiders + aceleración + agentes LLM con citas verificadas</p></div>',
        unsafe_allow_html=True,
    )


def vista_panorama():
    corte = leer_corte()

    totales = query_df("""
        select
            count(*)                                            as total,
            count(*) FILTER (where activa)                      as activas,
            count(*) FILTER (where estado = 'filtros_ok')       as filtros_ok,
            count(*) FILTER (where estado = 'scoring_ok')       as scoring_ok,
            count(*) FILTER (where estado = 'analizada')        as analizadas
        from empresas
    """).iloc[0]

    candidatas_llm = query_df(
        """
        select count(distinct empresa_id) as n
        from auditorias where veredicto is null and score_total >= %s
        """, (corte,)
    ).iloc[0]["n"]

    seguras = query_df(
        "select count(distinct empresa_id) as n from auditorias where veredicto = 'MUY_INTERESANTE'"
    ).iloc[0]["n"]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Empresas descubiertas", f"{totales.total:,}")
    c2.metric("Activas", f"{totales.activas:,}")
    c3.metric("Pasaron Capa 1", f"{totales.filtros_ok + totales.scoring_ok:,}")
    c4.metric(f"Candidatas LLM (≥{corte}/40)", f"{candidatas_llm:,}")
    c5.metric("Analizadas por agentes", f"{totales.analizadas:,}")
    c6.metric("Muy interesantes", f"{seguras:,}")

    st.divider()

    izq, der = st.columns([3, 2])

    with izq:
        st.subheader("Ranking Capa 2")
        ranking = query_df(
            """
            select e.ticker, e.nombre, e.sector, e.market_cap_usd,
                   a.score_precio as precio, a.score_conviccion as conviccion,
                   a.score_temporal as temporal, a.score_catalizador as catalizador,
                   a.score_total as total
            from auditorias a
            join empresas e on e.id = a.empresa_id
            where a.veredicto is null
            order by a.score_total desc
            limit 50
            """
        )
        if ranking.empty:
            st.info("Todavía no hay scoring. Ejecuta filtro_capa1.py y scorer_capa2.py.")
        else:
            ranking["market_cap_usd"] = ranking["market_cap_usd"].map(
                lambda v: f"${v/1e6:,.0f}M" if pd.notna(v) else "—"
            )
            st.dataframe(
                ranking,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "total": st.column_config.ProgressColumn(
                        "total /40", min_value=0, max_value=40, format="%d"
                    ),
                },
            )

    with der:
        st.subheader("Últimos veredictos de los agentes")
        veredictos = query_df(
            """
            select e.ticker, a.modelo_llm as modelo, a.veredicto,
                   a.fecha_analisis::DATE as fecha
            from auditorias a
            join empresas e on e.id = a.empresa_id
            where a.veredicto is not null
            order by a.fecha_analisis desc
            limit 20
            """
        )
        if veredictos.empty:
            st.info("Aún no hay análisis LLM. Ejecuta detective.py sobre alguna candidata.")
        else:
            for r in veredictos.itertuples():
                st.markdown(
                    f"**{r.ticker}** · {r.modelo} · {r.fecha} → "
                    f"<span class='veredicto-{r.veredicto}'>{r.veredicto}</span>",
                    unsafe_allow_html=True,
                )


# Un análisis de más de esto se marca como posiblemente desfasado: los
# puntos fuertes/débiles describen la empresa el día que se analizó, no hoy.
DIAS_ANALISIS_ANTIGUO = 90

ETIQUETA_VEREDICTO = {
    "MUY_INTERESANTE": "🟢 MUY_INTERESANTE",
    "INTERESANTE": "🟠 INTERESANTE",
    "NADA_INTERESANTE": "🔴 NADA_INTERESANTE",
    "ALUCINACION": "🟣 ALUCINACION",
}
# Las que aún no han pasado por los agentes van entre INTERESANTE y
# NADA_INTERESANTE: un score alto sin juzgar todavía merece más atención
# que algo que un agente ya descartó, pero no que algo ya confirmado.
ORDEN_VEREDICTO = {
    "MUY_INTERESANTE": 0, "INTERESANTE": 1, "SIN_ANALIZAR": 2,
    "NADA_INTERESANTE": 3, "ALUCINACION": 4,
}


def _dias_desde(fecha):
    """Días transcurridos desde una fecha o timestamp; None si no hay fecha."""
    if fecha is None or pd.isna(fecha):
        return None
    return (pd.Timestamp.now().normalize() - pd.Timestamp(fecha).normalize()).days


def _hace(fecha) -> str:
    dias = _dias_desde(fecha)
    if dias is None:
        return "—"
    return f"hace {dias} d" if dias < 365 else f"hace {dias / 365:.1f} años"


def _texto_analisis(fecha) -> str:
    dias = _dias_desde(fecha)
    if dias is None:
        return "—"
    aviso = "⚠️ " if dias > DIAS_ANALISIS_ANTIGUO else ""
    return f"{aviso}{fecha:%Y-%m-%d} ({_hace(fecha)})"


def _leer_candidatas(corte_llm: int) -> pd.DataFrame:
    """Todas las empresas con veredicto, más las que tienen score >=
    corte_llm aunque ningún agente las haya analizado todavía."""
    return query_df(
        """
        with ids as (
            select empresa_id from auditorias where veredicto is not null
            union
            select empresa_id from auditorias
            where veredicto is null and score_total >= %s
        )
        select e.id, e.ticker, e.nombre, e.sector,
               ultimo.veredicto, ultimo.fecha_analisis,
               capa2.score_total,
               ins.ultima_compra
        from ids
        join empresas e on e.id = ids.empresa_id
        left join lateral (
            select veredicto, fecha_analisis from auditorias
            where empresa_id = e.id and veredicto is not null
            order by fecha_analisis desc limit 1
        ) ultimo on true
        left join lateral (
            select score_total from auditorias
            where empresa_id = e.id and veredicto is null
            order by fecha_analisis desc limit 1
        ) capa2 on true
        left join lateral (
            select max(fecha_transaccion) as ultima_compra
            from insider_transactions
            where empresa_id = e.id and tipo_transaccion = 'P'
        ) ins on true
        """,
        (corte_llm,),
    )


def _tabla_candidatas(df: pd.DataFrame, con_score: bool) -> pd.DataFrame:
    tabla = pd.DataFrame({
        "Ticker": df["ticker"],
        "Empresa": df["nombre"],
        "Veredicto": df["veredicto"].map(ETIQUETA_VEREDICTO).fillna("⚪ sin analizar"),
        "Último análisis": df["fecha_analisis"].map(_texto_analisis),
        "Última compra de insiders": df["ultima_compra"].map(
            lambda f: "—" if pd.isna(f) else f"{f:%Y-%m-%d} ({_hace(f)})"
        ),
    })
    if con_score:
        tabla.insert(3, "Score /40", df["score_total"])
    return tabla


def _detalle_candidata(conn, r, con_score: bool):
    empresa_id = int(r["id"])
    ticker = r["ticker"]
    veredicto = r["veredicto"] if isinstance(r["veredicto"], str) else None

    with st.container(border=True):
        st.markdown(f"#### {ticker} — {r['nombre']}")
        score = f"score {int(r['score_total'])}/40" if con_score else "sin score vigente"
        etiqueta = (
            f"<span class='veredicto-{veredicto}'>{veredicto}</span>"
            if veredicto else "sin analizar"
        )
        st.markdown(
            f"{r['sector'] or '—'} · {score} · {etiqueta} · última compra de "
            f"insiders: {_hace(r['ultima_compra'])}",
            unsafe_allow_html=True,
        )

        # La generación va detrás de un botón a propósito: Streamlit
        # ejecuta el código de la página en CADA rerun, y llamar aquí
        # directo a un LLM disparaba una petición por empresa sin resumen,
        # todas seguidas, y saturaba la cuota (429 en cascada, visto en
        # real). La comprobación de caché sí es solo una lectura de BD.
        st.markdown("**A qué se dedica**")
        cur = conn.cursor()
        cur.execute("select resumen_negocio from empresas where id = %s", (empresa_id,))
        resumen_cacheado = cur.fetchone()[0]
        cur.close()

        if resumen_cacheado:
            st.write(resumen_cacheado)
        elif st.button("Generar resumen del negocio", key=f"resumen_{empresa_id}"):
            contexto = obtener_contexto_empresa(conn, ticker)
            with st.spinner("Generando..."):
                st.write(obtener_resumen_negocio(conn, empresa_id, contexto))
        else:
            st.caption("Pulsa el botón para generarlo (una sola vez, queda guardado).")

        # Un análisis por modelo (los dos Detectives son votos ciegos y
        # pueden discrepar, y cada uno tiene su fecha), el más reciente
        # de cada uno.
        analisis = query_df(
            """select modelo_llm, fecha_analisis, respuesta_llm
               from auditorias
               where empresa_id = %s and respuesta_llm ? 'puntos_fuertes'
               order by fecha_analisis desc""",
            (empresa_id,)
        )
        if analisis.empty:
            st.info("Todavía sin análisis de los agentes.")
            return

        for a in analisis.drop_duplicates("modelo_llm").itertuples():
            resp = a.respuesta_llm if isinstance(a.respuesta_llm, dict) else json.loads(a.respuesta_llm or "{}")
            st.markdown(
                f"**Detective {a.modelo_llm}** · análisis del "
                f"{a.fecha_analisis:%Y-%m-%d} ({_hace(a.fecha_analisis)})"
            )
            dias = _dias_desde(a.fecha_analisis)
            if dias is not None and dias > DIAS_ANALISIS_ANTIGUO:
                st.warning(
                    f"Este análisis tiene {dias} días: puede no reflejar la "
                    "situación actual de la empresa."
                )
            for titulo, clave_json in (
                ("Puntos fuertes", "puntos_fuertes"),
                ("Puntos débiles", "puntos_debiles"),
            ):
                puntos = resp.get(clave_json) or []
                if puntos:
                    st.markdown(f"**{titulo}**\n" + "\n".join(f"- {p}" for p in puntos))


def _bloque_candidatas(conn, df: pd.DataFrame, clave: str, con_score: bool):
    """Tabla con selección de una fila y, debajo, el detalle de esa empresa."""
    df = df.reset_index(drop=True)
    evento = st.dataframe(
        _tabla_candidatas(df, con_score),
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        # len(df) en la clave: si cambia la lista (p. ej. al marcar el
        # checkbox) la selección anterior ya no apunta a la misma fila.
        key=f"tabla_{clave}_{len(df)}",
        column_config=(
            {"Score /40": st.column_config.ProgressColumn(
                "Score /40", min_value=0, max_value=40, format="%d"
            )} if con_score else None
        ),
    )
    filas = evento.selection.rows
    if filas and filas[0] < len(df):
        _detalle_candidata(conn, df.iloc[filas[0]], con_score)
    else:
        st.caption("Selecciona una fila para ver el resumen, los puntos fuertes y los débiles.")


def vista_candidatas():
    st.subheader("🏆 Candidatas por interés")
    corte = leer_corte()
    incluir_bajo_corte = st.checkbox(
        f"Incluir también las que no llegan al corte de LLM (score < {corte}/40)"
    )
    st.caption(
        "Ordenadas por veredicto y luego por score. Las que aún no han pasado "
        "por los agentes salen como «sin analizar». Selecciona una fila para "
        "ver el detalle."
    )

    # score_total y veredicto viven en filas DISTINTAS: scorer_capa2.py
    # crea la fila con el score real (veredicto is null), y el
    # Detective/Auditor insertan una fila aparte con el veredicto pero
    # score_total a 0 por defecto -- mismo patrón que ya separa
    # vista_panorama() en dos queries.
    #
    # Una empresa con veredicto pero sin fila de score es una que Capa 1
    # descartó después (su cluster de insiders ya no entra en el horizonte
    # de recencia): pierde su score a propósito, para que el orquestador
    # no siga gastando LLM en ella. Va a un bloque aparte, abajo -- si se
    # mezclara, su veredicto viejo la pondría la primera.
    candidatas = _leer_candidatas(0 if incluir_bajo_corte else corte)
    if candidatas.empty:
        st.info("Todavía no hay candidatas ni veredictos.")
        return

    candidatas["orden"] = candidatas["veredicto"].fillna("SIN_ANALIZAR").map(ORDEN_VEREDICTO)
    vigentes = candidatas[candidatas["score_total"].notna()].sort_values(
        ["orden", "score_total"], ascending=[True, False]
    )
    antiguas = candidatas[candidatas["score_total"].isna()].sort_values(["orden", "ticker"])

    conn = conectar_db()
    if vigentes.empty:
        st.info("Ninguna empresa pasa ahora mismo la Capa 1 con score suficiente.")
    else:
        _bloque_candidatas(conn, vigentes, "vigentes", con_score=True)

    if not antiguas.empty:
        st.divider()
        st.subheader("Señal de insiders antigua (fuera de Capa 1)")
        st.caption(
            "Ya tienen veredicto, pero su cluster de insiders no entra en el "
            "horizonte de recencia de Capa 1 (12 meses por defecto), así que "
            "el pipeline ya no las trata como candidatas. Solo para consulta."
        )
        _bloque_candidatas(conn, antiguas, "antiguas", con_score=False)


def vista_empresa(ticker: str):
    conn = conectar_db()
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        st.error(f"{ticker} no está en la base de datos")
        return

    sp, sc, st_, scat, stotal = contexto["scores"] or (0, 0, 0, 0, 0)

    st.subheader(f"{contexto['nombre']} ({ticker})")
    st.caption(
        f"{contexto['sector'] or 'sector desconocido'} · {contexto['bolsa'] or '—'} · "
        f"market cap ${(contexto['market_cap'] or 0):,.0f}"
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Precio 52w", f"{sp}/10")
    m2.metric("Convicción", f"{sc}/10")
    m3.metric("Aceleración", f"{st_}/10")
    m4.metric("Catalizador", f"{scat}/10")
    m5.metric("TOTAL", f"{stotal}/40")

    izq, der = st.columns(2)

    with izq:
        st.markdown("##### Revenue trimestral")
        serie = query_df(
            """
            select anio_fiscal || ' Q' || trimestre as trimestre, revenue, gross_profit
            from metricas_trimestrales mt
            join empresas e on e.id = mt.empresa_id
            where e.ticker = %s and revenue is not null
            order by anio_fiscal, mt.trimestre
            """,
            (ticker,)
        )
        if serie.empty:
            st.info("Sin métricas trimestrales para esta empresa.")
        else:
            st.bar_chart(serie.set_index("trimestre")["revenue"])
            con_margen = serie.dropna(subset=["gross_profit"]).copy()
            if not con_margen.empty:
                con_margen["margen bruto %"] = (
                    100 * con_margen["gross_profit"] / con_margen["revenue"]
                ).round(1)
                st.markdown("##### Margen bruto (%)")
                st.line_chart(con_margen.set_index("trimestre")["margen bruto %"])

    with der:
        st.markdown("##### Compras de insiders (últimas 10)")
        compras = pd.DataFrame(
            contexto["transacciones"],
            columns=["insider", "cargo", "fecha", "importe"],
        )
        if compras.empty:
            st.info("Sin compras P registradas.")
        else:
            compras["importe"] = compras["importe"].map(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
            )
            st.dataframe(compras, use_container_width=True, hide_index=True)

    if contexto["texto_mda"]:
        anio, trim = contexto["mda_anio_trim"]
        with st.expander(f"MD&A del 10-Q ({anio} Q{trim}) — el texto que leen los agentes"):
            st.text(contexto["texto_mda"][:15000])

    st.divider()
    st.markdown("##### Análisis de los agentes")
    analisis = query_df(
        """
        select a.modelo_llm, a.veredicto, a.fecha_analisis,
               a.respuesta_llm, a.verificacion_citas
        from auditorias a join empresas e on e.id = a.empresa_id
        where e.ticker = %s and a.veredicto is not null
        order by a.fecha_analisis desc
        """,
        (ticker,)
    )
    if analisis.empty:
        st.info(
            f"Sin análisis LLM todavía. Lánzalo con: "
            f"`python detective.py --ticker {ticker} --modelo groq`"
        )
    for r in analisis.itertuples():
        # psycopg2 devuelve JSONB como dict; si viniera como texto, lo parseo
        resp = r.respuesta_llm if isinstance(r.respuesta_llm, dict) else json.loads(r.respuesta_llm or "{}")
        verif = r.verificacion_citas if isinstance(r.verificacion_citas, dict) else json.loads(r.verificacion_citas or "{}")
        es_auditor = "veredicto_auditor" in resp

        with st.container(border=True):
            st.markdown(
                f"**{'Auditor' if es_auditor else 'Detective'}** · {r.modelo_llm} · "
                f"{r.fecha_analisis:%Y-%m-%d %H:%M} → "
                f"<span class='veredicto-{r.veredicto}'>{r.veredicto}</span> · "
                f"citas verificadas {verif.get('citas_verificadas', '—')}/{verif.get('citas_totales', '—')}",
                unsafe_allow_html=True,
            )
            if es_auditor:
                st.write(resp.get("justificacion", ""))
                if resp.get("riesgos_omitidos_por_detective"):
                    st.write("Riesgos que el Detective no vio: " +
                             "; ".join(resp["riesgos_omitidos_por_detective"]))
                if resp.get("fortalezas_omitidas_por_detective"):
                    st.write("Fortalezas que el Detective no vio: " +
                             "; ".join(resp["fortalezas_omitidas_por_detective"]))
            else:
                if resp.get("catalizador_no_obvio"):
                    st.write(f"**Catalizador:** {resp['catalizador_no_obvio']}")
                if resp.get("tesis_inversion"):
                    st.write(f"**Tesis:** {resp['tesis_inversion']}")
                if resp.get("riesgos"):
                    st.write("**Riesgos:** " + "; ".join(resp["riesgos"]))


def vista_chat(ticker: str, modelo: str):
    st.subheader(f"💬 Chat sobre {ticker}")
    st.caption(
        "El chat ve lo mismo que el Detective: scores, insiders, MD&A y "
        "veredictos. No pasa por el verificador de citas — para el "
        "veredicto oficial, mira la ficha de la empresa."
    )

    # Historial por ticker: si cambio de empresa, empiezo conversación nueva
    clave = f"chat_{ticker}"
    if clave not in st.session_state:
        st.session_state[clave] = []

    conn = conectar_db()
    sistema = construir_contexto_chat(conn, ticker)
    if sistema is None:
        st.error(f"{ticker} no está en la base de datos")
        return

    for msg in st.session_state[clave]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    pregunta = st.chat_input("Pregunta lo que quieras sobre esta empresa...")
    if pregunta:
        st.session_state[clave].append({"role": "user", "content": pregunta})
        with st.chat_message("user"):
            st.write(pregunta)

        mensajes = (
            [{"role": "system", "content": sistema}]
            + st.session_state[clave]
        )
        with st.chat_message("assistant"):
            with st.spinner(f"Pensando ({modelo})..."):
                try:
                    if modelo == "groq":
                        respuesta = chat_groq(mensajes)
                    else:
                        respuesta = chat_gemini(mensajes)
                except Exception as e:
                    respuesta = f"Error llamando a {modelo}: {e}"
            st.write(respuesta)
        st.session_state[clave].append({"role": "assistant", "content": respuesta})


# ---------- estructura principal ----------

def main():
    cabecera()

    with st.sidebar:
        st.markdown("### Navegación")
        vista = st.radio(
            "Vista", ["📊 Panorama", "🏆 Candidatas", "🔎 Empresa", "💬 Chat"],
            label_visibility="collapsed",
        )

        ticker = None
        modelo = "groq"
        if vista in ("🔎 Empresa", "💬 Chat"):
            # Ofrezco primero las candidatas con mejor score — que son
            # las que de verdad interesa mirar — pero cualquier empresa
            # de la BD se puede escribir a mano.
            interesantes = query_df(
                """
                select e.ticker
                from auditorias a join empresas e on e.id = a.empresa_id
                where a.veredicto is null
                order by a.score_total desc
                limit 200
                """
            )
            opciones = interesantes["ticker"].tolist() if not interesantes.empty else []
            ticker = st.selectbox(
                "Empresa (ordenadas por score)", opciones,
                index=0 if opciones else None,
                accept_new_options=True,
            )

        if vista == "💬 Chat":
            modelo = st.radio("Modelo del chat", ["groq", "gemini"], horizontal=True)

        st.divider()
        if st.button("Probar alerta de Telegram"):
            ok = enviar_telegram("🦄 SmallCap Hunter: prueba de alerta desde el dashboard")
            if ok:
                st.success("Enviada — mira tu Telegram")
            else:
                st.warning(
                    "No se envió. ¿TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID están en el entorno?"
                )
        st.caption("Los avisos automáticos los mandan detective.py y auditor.py al terminar cada análisis.")

    if vista == "📊 Panorama":
        vista_panorama()
    elif vista == "🏆 Candidatas":
        vista_candidatas()
    elif vista == "🔎 Empresa" and ticker:
        vista_empresa(ticker)
    elif vista == "💬 Chat" and ticker:
        vista_chat(ticker, modelo)
    else:
        st.info("Selecciona una empresa en la barra lateral.")


if __name__ == "__main__":
    main()
