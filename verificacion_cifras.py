"""
verificacion_cifras.py — la parte de CÓDIGO del Auditor: comprobar las cifras.

Una tesis de inversión está hecha de afirmaciones con números («los ingresos
crecieron un 34%», «el CFO compró $512K»). Un modelo puede equivocarse en un número
o inventárselo, y un lector humano no lo nota sin releer el documento. Esto lo hace
un programa, gratis y siempre igual:

  - saca de la tesis cada porcentaje e importe en dólares;
  - mira si ese número aparece en el texto original (con tolerancia de miles y
    millones: «$1.2 million» en la tesis, «1,200» en una tabla en miles);
  - las cifras que NO aparecen se marcan. No todas son errores: un porcentaje
    calculado a partir de otras cifras del texto (un crecimiento, una suma) no sale
    tal cual en ningún sitio. Por eso el modelo puede ABSOLVER una cifra marcada,
    pero solo si enseña un cálculo cuyos operandos estén de verdad en el texto
    (`derivacion_verificable`): el modelo propone, el código comprueba.

Medido en una prueba sintética de 20 casos (10 con una cifra alterada a mano y 10 sin
tocar, en un prototipo que no se versiona): detecta 10 de 10, con 1–2 falsas alarmas
sobre 10. Sobre análisis reales marca un 19%, casi todo cifras derivadas. Son muestras
pequeñas: las falsas alarmas a escala aún no están medidas.
"""

import re

PCT = re.compile(r"\b\d{1,3}(?:\.\d+)?%")
USD = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(million|billion|thousand)?", re.I)
NUM = re.compile(r"\d[\d,]*\.?\d*")
ESCALAS = (1, 1e3, 1e-3, 1e6, 1e-6, 1e9, 1e-9)
UNIDADES = {"million": 1e6, "billion": 1e9, "thousand": 1e3}


def valores_del_texto(texto: str) -> set:
    """Todos los números que aparecen en el texto, como floats."""
    out = set()
    for m in NUM.findall(texto or ""):
        try:
            out.add(float(m.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    return out


def aparece(valor: float, fuente: set) -> bool:
    """¿Está `valor` entre los números de la fuente, en cualquier escala (unidades,
    miles, millones, miles de millones)? La tolerancia cubre el redondeo."""
    tol = max(0.05, 0.006 * abs(valor))
    return any(abs(s * k - valor) <= tol for s in fuente for k in ESCALAS)


def cifras_sin_soporte(tesis: str, fuente: str) -> list:
    """Cifras de la tesis (porcentajes e importes) que no aparecen en la fuente:
    [{'cifra': '34%', 'contexto': '...texto alrededor...'}]. Se ignoran los valores
    pequeños (<5) y los años, que no son cifras que comprobar."""
    fv = valores_del_texto(fuente)
    candidatas = []
    for m in PCT.finditer(tesis or ""):
        candidatas.append((m.group(0), float(m.group(0)[:-1]), m.start()))
    for m in USD.finditer(tesis or ""):
        valor = float(m.group(1).replace(",", "")) * UNIDADES.get((m.group(2) or "").lower(), 1)
        candidatas.append((m.group(0).strip(), valor, m.start()))
    sospechosas = []
    for texto, valor, pos in candidatas:
        if valor < 5 or 1900 <= valor <= 2100 or aparece(valor, fv):
            continue
        contexto = tesis[max(0, pos - 60): pos + len(texto) + 40].replace("\n", " ")
        sospechosas.append({"cifra": texto, "contexto": contexto})
    return sospechosas


def derivacion_verificable(explicacion: str, cifra: str, fuente_valores: set) -> bool:
    """El modelo solo puede ABSOLVER una cifra que el código marcó si su cálculo se
    apoya en números que están de verdad en el texto: al menos dos operandos (sin
    contar la propia cifra) que aparezcan en la fuente."""
    propia = None
    m = re.search(r"\d[\d,]*\.?\d*", cifra or "")
    if m:
        try:
            propia = float(m.group(0).replace(",", "").rstrip("."))
        except ValueError:
            pass
    operandos = []
    for n in NUM.findall(explicacion or ""):
        try:
            v = float(n.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if propia is not None and abs(v - propia) <= max(0.06, 0.006 * propia):
            continue  # el resultado, no un operando
        if v >= 3 and not 1900 <= v <= 2100:
            operandos.append(v)
    return sum(1 for v in operandos if aparece(v, fuente_valores)) >= 2


def bloque_estructurado(contexto: dict) -> str:
    """Lo que el Detective ve y el texto del 10-Q no trae: sector, market cap, scores y
    la lista de compras de insiders. Sin esto, cualquier cifra de la tesis que venga de
    ahí («el CFO compró $512K») parece inventada. Es fuente válida para el código y
    para las citas. Tolera datos que falten."""
    sp, sc, st, scat, stotal = contexto.get("scores") or (0, 0, 0, 0, 0)
    cap = contexto.get("market_cap")
    cap_texto = f"${cap:,.0f}" if cap else "no disponible"
    insiders = "\n".join(
        f"  - {n} ({cargo or 'cargo no especificado'}): ${imp:,.0f} el {f}"
        for n, cargo, f, imp in (contexto.get("transacciones") or []) if imp is not None
    ) or "  (sin detalle de importes disponible)"
    return f"""DATOS ESTRUCTURADOS DE LA EMPRESA (los mismos que vio el Detective; son fuente válida):
  Sector: {contexto.get('sector')} | Bolsa: {contexto.get('bolsa')} | Market cap actual: {cap_texto}
  Scores (0-10): posición en rango 52 semanas {sp}, convicción de insiders {sc}, aceleración de crecimiento {st}, mejora de catalizador {scat}; TOTAL {stotal}/40
COMPRAS DE INSIDERS RECIENTES:
{insiders}"""
