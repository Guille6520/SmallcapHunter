"""
verificaciones.py — el resultado del Auditor: un sello por análisis, no un veredicto.

El Auditor de la v1 daba su propio veredicto (MUY_INTERESANTE / INTERESANTE /
NADA_INTERESANTE) sobre cada análisis, y ese veredicto era ruido: declaró EMPEORA en
74 % de los casos (96 de 130) y su veredicto real fue más bajo que el del Detective en
45 %, también cuando auditaba a su propio modelo. Un LLM no puede dar un juicio global
estable sobre otro análisis.

El Auditor de la v2 solo VERIFICA. Para cada análisis de un Detective comprueba qué
parte de lo que dice está respaldada por el texto original, y el resultado es un sello:

  VERIFICADO   las cifras de la tesis están en el texto (o se derivan de él con un
               cálculo comprobado) y ninguna afirmación contradice al documento;
  CON_AVISOS   hay cifras que no se sostienen, o una afirmación que el texto contradice
               con una cita real. El aviso dice cuál, para que se lea a mano.

El sello NO cambia el veredicto del Detective ni lo sustituye: dice cuánto fiarse de él.
El veredicto de una empresa son los votos de los Detectives.

Se guarda en su propia tabla (`verificaciones`) y no en `auditorias`, porque allí una
fila con `veredicto` nulo es una fila de score (el scorer las borra y reescribe) y una
con `veredicto` es un análisis (lo cuentan el dashboard, la API y el orquestador).
Las auditorías de la v1 siguen en `auditorias`, intactas, como histórico.
"""

import json
import re

from verificacion_cifras import derivacion_verificable, valores_del_texto

VERSION_AUDITOR = 2
SELLO_VERIFICADO = "VERIFICADO"
SELLO_CON_AVISOS = "CON_AVISOS"

DDL = """
create table if not exists verificaciones (
    id                      serial primary key,
    empresa_id              integer not null references empresas(id) on delete cascade,
    detective_auditoria_id  integer not null references auditorias(id) on delete cascade,
    modelo_detective        text    not null,
    modelo_auditor          text    not null,
    version_auditor         integer not null,
    sello                   text    not null,
    resumen                 jsonb   not null,
    respuesta_llm           jsonb   not null,
    verificacion_citas      jsonb,
    fecha                   timestamp default now(),
    unique (detective_auditoria_id, modelo_auditor, version_auditor)
);
create index if not exists verificaciones_empresa_idx on verificaciones (empresa_id);
"""

_esquema_listo = False


def asegurar_esquema(conn):
    """Crea la tabla si no existe. Es barato y se hace una vez por proceso, así que lo
    llama quien lee o escribe sin depender de que alguien lo haya ejecutado antes."""
    global _esquema_listo
    if _esquema_listo:
        return
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        conn.commit()
        _esquema_listo = True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def ya_verificado(conn, detective_auditoria_id: int, modelo_auditor: str) -> bool:
    """¿Ya hay un sello de este análisis hecho por este modelo, con esta versión del
    Auditor? Evita pagar otra vez una auditoría ya hecha si se relanza el script."""
    asegurar_esquema(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select 1 from verificaciones
            where detective_auditoria_id = %s and modelo_auditor = %s and version_auditor = %s
            limit 1
            """,
            (detective_auditoria_id, modelo_auditor, VERSION_AUDITOR),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()


def guardar(conn, r: dict) -> bool:
    """Guarda un sello. True si quedó guardado, False si no (para que quien llama no
    cuente como hecha una auditoría que se pagó y se perdió)."""
    asegurar_esquema(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            insert into verificaciones (
                empresa_id, detective_auditoria_id, modelo_detective, modelo_auditor,
                version_auditor, sello, resumen, respuesta_llm, verificacion_citas
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (detective_auditoria_id, modelo_auditor, version_auditor) do nothing
            """,
            (
                r["empresa_id"], r["detective_auditoria_id"], r["modelo_detective"], r["modelo_auditor"],
                VERSION_AUDITOR, r["sello"], json.dumps(r["resumen"]),
                json.dumps(r["respuesta_llm"]), json.dumps(r["verificacion_citas"]),
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# ---------- decidir el sello (puro, sin base de datos ni modelos) ----------

def citas_del_auditor(resp: dict) -> list:
    """Todas las citas que el modelo dice haber sacado del documento, para verificarlas
    de una vez: las de afirmaciones con respaldo o contradichas, las de cifras
    contradichas y las de riesgos omitidos. Una afirmación SIN_RESPALDO no lleva cita."""
    citas = []
    for a in resp.get("afirmaciones") or []:
        if a.get("estado") != "SIN_RESPALDO" and a.get("cita_literal"):
            citas.append({"afirmacion": a.get("afirmacion", ""), "cita_literal": a["cita_literal"]})
    for c in resp.get("cifras") or []:
        if c.get("estado") == "CONTRADICHA" and c.get("cita_literal"):
            citas.append({"afirmacion": c.get("cifra", ""), "cita_literal": c["cita_literal"]})
    for r in resp.get("riesgos_omitidos") or []:
        if r.get("cita_literal"):
            citas.append({"afirmacion": r.get("riesgo", ""), "cita_literal": r["cita_literal"]})
    return citas


def _normalizar_cifra(x) -> str:
    return re.sub(r"[^\d.%$]", "", str(x))


def valorar(resp: dict, fuente_texto: str, sospechosas: list, verificacion_citas: dict) -> dict:
    """Pasa por código lo que dijo el modelo, y decide el sello. El modelo no da veredicto:
      - El código marcó las cifras que no están en el texto (`sospechosas`).
      - El modelo puede absolver una (DERIVADA_CORRECTA) SOLO si su cálculo se apoya en
        números que están en el texto; si no, la cifra sigue marcada.
      - SIN_SOPORTE o CONTRADICHA confirman la marca del código, y una cifra marcada
        que el modelo ni menciona sigue marcada (sin adjudicar).
      - Una afirmación contradicha cuenta solo si su cita existe de verdad en el texto.
      - Un riesgo omitido solo se enseña si su cita existe de verdad en el texto.
    `verificacion_citas` es el resultado de verificar_citas sobre citas_del_auditor(resp)."""
    fv = valores_del_texto(fuente_texto)
    soportadas = {d["cita_literal"] for d in verificacion_citas.get("detalle", []) if d.get("soportada")}

    def cita_ok(c) -> bool:
        return bool(c) and c in soportadas

    afirmaciones = resp.get("afirmaciones") or []
    # Solo cuentan las cifras que el código marcó; las que el modelo comente por su cuenta se ignoran
    claves = [_normalizar_cifra(s["cifra"]) for s in sospechosas]
    cifras = [c for c in (resp.get("cifras") or [])
              if any(k and (k in _normalizar_cifra(c.get("cifra", "")) or _normalizar_cifra(c.get("cifra", "")) in k)
                     for k in claves)]
    absueltas = [c for c in cifras if c.get("estado") == "DERIVADA_CORRECTA"
                 and derivacion_verificable(c.get("explicacion"), c.get("cifra", ""), fv)]
    absueltas_sin_base = [c for c in cifras if c.get("estado") == "DERIVADA_CORRECTA" and c not in absueltas]
    confirmadas = [c for c in cifras if c.get("estado") in ("SIN_SOPORTE", "CONTRADICHA")]
    cubiertas = {_normalizar_cifra(c.get("cifra", "")) for c in cifras}
    sin_adjudicar = [s for s in sospechosas
                     if not any(_normalizar_cifra(s["cifra"]) in m or m in _normalizar_cifra(s["cifra"])
                                for m in cubiertas if m)]

    contradichas = [a for a in afirmaciones if a.get("estado") == "CONTRADICHA" and cita_ok(a.get("cita_literal"))]
    riesgos = [r for r in (resp.get("riesgos_omitidos") or []) if cita_ok(r.get("cita_literal"))]

    avisos_cifras = (
        [{"cifra": c.get("cifra"), "motivo": "contradicha" if c.get("estado") == "CONTRADICHA" else "sin_soporte",
          "explicacion": c.get("explicacion", ""), "cita_literal": c.get("cita_literal", "")} for c in confirmadas]
        + [{"cifra": c.get("cifra"), "motivo": "derivacion_sin_base",
            "explicacion": c.get("explicacion", ""), "cita_literal": ""} for c in absueltas_sin_base]
        + [{"cifra": s["cifra"], "motivo": "sin_adjudicar", "explicacion": "", "cita_literal": "",
            "contexto": s["contexto"]} for s in sin_adjudicar]
    )

    con_avisos = bool(avisos_cifras or contradichas)
    return {
        "sello": SELLO_CON_AVISOS if con_avisos else SELLO_VERIFICADO,
        "n_afirmaciones": len(afirmaciones),
        "respaldadas": sum(1 for a in afirmaciones if a.get("estado") == "RESPALDADA"),
        "sin_respaldo": sum(1 for a in afirmaciones if a.get("estado") == "SIN_RESPALDO"),
        "respaldadas_con_cita_falsa": sum(1 for a in afirmaciones
                                           if a.get("estado") == "RESPALDADA" and not cita_ok(a.get("cita_literal"))),
        "cifras_marcadas_por_codigo": len(sospechosas),
        "cifras_absueltas": len(absueltas),
        "avisos_cifras": avisos_cifras,
        "afirmaciones_contradichas": [{"afirmacion": a.get("afirmacion"), "cita_literal": a.get("cita_literal")}
                                       for a in contradichas],
        "riesgos_omitidos": [{"riesgo": r.get("riesgo"), "cita_literal": r.get("cita_literal")} for r in riesgos],
        "n_avisos": len(avisos_cifras) + len(contradichas),
    }
