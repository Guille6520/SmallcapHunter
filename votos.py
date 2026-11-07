"""
votos.py — qué veredicto enseña el dashboard de una empresa.

Cada empresa tiene hasta dos votos ciegos de Detective (claude y gemini) y,
después, las auditorías cruzadas. Durante un tiempo el dashboard enseñaba «el
último veredicto guardado», y como el Auditor guarda su fila después de los
Detectives, en 39 de 55 empresas lo que se veía era el del Auditor, que declaraba
EMPEORA en 74 % de los casos y cuyo veredicto real era más bajo en 45 % (ver el
README): salía 1 empresa MUY_INTERESANTE
donde los Detectives daban 18 de 55 (cifras a 7-nov-2026). El veredicto de una empresa es el de sus Detectives;
el Auditor queda como nota aparte en la ficha.

Está aquí y no en dashboard.py porque dashboard.py ejecuta Streamlit al importarlo y
no se puede probar. Las reglas, sin base de datos, están en tests/test_votos.py.
"""

# Orden de más a menos interesante. «SIN_ANALIZAR» va entre INTERESANTE y
# NADA_INTERESANTE: un score alto sin juzgar todavía merece más atención que algo que
# un agente ya descartó, pero no que algo ya confirmado.
ORDEN_VEREDICTO = {
    "MUY_INTERESANTE": 0, "INTERESANTE": 1, "SIN_ANALIZAR": 2,
    "NADA_INTERESANTE": 3, "ALUCINACION": 4,
}


def mejor_voto(*votos):
    """El voto más favorable de los que hay, o None si no hay ninguno. Ordena la
    lista: una empresa a la que un Detective da MUY_INTERESANTE merece mirarse
    aunque el otro no la vea, y el desacuerdo se ve en las dos columnas de voto."""
    validos = [v for v in votos if isinstance(v, str) and v in ORDEN_VEREDICTO]
    return min(validos, key=ORDEN_VEREDICTO.get) if validos else None


MARCA_SELLO = {"VERIFICADO": "✅", "CON_AVISOS": "⚠️"}


def marca_sello(sello) -> str:
    """El icono del sello del Auditor para la lista, o nada si el análisis aún no se ha
    verificado. El sello no cambia el voto: dice cuánto fiarse de él."""
    return MARCA_SELLO.get(sello, "") if isinstance(sello, str) else ""


def texto_sello(sello, n_avisos=0, modelo_auditor=None) -> str:
    """La línea del sello en la ficha: «✅ Verificado por gemini» o «⚠️ 2 avisos según
    gemini». Sin sello, dice que falta, no lo omite."""
    quien = f" por {modelo_auditor}" if isinstance(modelo_auditor, str) else ""
    if sello == "VERIFICADO":
        return f"{MARCA_SELLO['VERIFICADO']} Verificado{quien}"
    if sello == "CON_AVISOS":
        n = int(n_avisos or 0)
        return f"{MARCA_SELLO['CON_AVISOS']} {n} aviso{'s' if n != 1 else ''}{quien.replace(' por', ' según')}"
    return "⏳ Sin verificar por el Auditor todavía"


def con_sello(etiqueta: str, sello) -> str:
    """«🟡 INTERESANTE» + sello → «🟡 INTERESANTE ✅» (para la columna de la lista)."""
    marca = marca_sello(sello)
    return f"{etiqueta} {marca}" if marca else etiqueta


def texto_votos(claude, gemini) -> str:
    """«Claude: MUY_INTERESANTE · Gemini: INTERESANTE» para la cabecera de la ficha;
    un voto que falta se escribe «sin voto», no se omite."""
    # pandas rellena el hueco con NaN, que es «verdadero»: no vale `x or 'sin voto'`.
    claude = claude if isinstance(claude, str) else "sin voto"
    gemini = gemini if isinstance(gemini, str) else "sin voto"
    return f"Claude: {claude} · Gemini: {gemini}"
