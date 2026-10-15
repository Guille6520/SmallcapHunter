"""
verificador_citas.py — comprueba, sin otro LLM, que las citas de un
análisis salen de verdad de los documentos que leyó el modelo.

Versión 2 del verificador. La v1 pedía que cada cita fuese una subcadena
EXACTA de la fuente y marcaba ALUCINACION si menos de la mitad lo era. Al
repasar a mano las citas "fallidas" de 30 análisis reales, la mayoría no
eran inventadas: eran traducciones al español (el prompt pedía responder
en español), fragmentos unidos con "...", guiones largos en vez de
cortos, líneas de la lista de insiders (que no están en el MD&A) o filas
de una tabla recompuestas. Un verificador que castiga eso mide el formato,
no la fidelidad.

Ahora cada cita cae en UNA categoría (campo "tipo" del detalle):

  soportadas   exacta                 subcadena literal (la regla de la v1)
               exacta_sin_puntuacion  igual, ignorando puntuación y guiones
               fragmentos             trozos unidos con "...", cada uno exacto
               insiders               línea de compras que coincide con el Form 4
               editada                casi literal (>= 85% de la cita cubierta)
               tabla                  cifras que en la fuente están juntas
  sin soporte  score_del_sistema      cita un score nuestro, no el documento
               traducida              texto en español: no se puede comprobar
               parafrasis             solo se parece en parte
               sin_soporte            no aparece nada parecido
               demasiado_corta

Qué NO detecta (y por eso sigue existiendo el Auditor): una cita real a la
que el modelo le cuelga una explicación inventada. Esto comprueba que la
evidencia existe, no que la interpretación sea correcta.

Guardo dos porcentajes: el estricto (solo "exacta", el mismo número que
medía la v1, para poder comparar épocas) y el de soportadas (el que decide
la etiqueta ALUCINACION).
"""

import re
import unicodedata
from collections import Counter, defaultdict

# Versión conjunta del protocolo (prompt + verificador). Se guarda en el
# JSON de cada análisis para que el orquestador sepa qué filas se hicieron
# con reglas antiguas y las trate como pendientes.
VERSION_PROTOCOLO = 2
VERSION_VERIFICADOR = 2

# Por debajo de este % de citas soportadas, el análisis se marca ALUCINACION.
UMBRAL_ALUCINACION = 50
# Entre UMBRAL_ALUCINACION y este valor, "citas débiles": aviso, sin tocar el veredicto.
UMBRAL_CITAS_DEBILES = 80

MIN_CARACTERES_CITA = 8
COBERTURA_EDITADA = 0.85
COBERTURA_PARAFRASIS = 0.4
# Dos cifras de una cita cuentan como "de la misma fila de tabla" si en la
# fuente están a menos de estos tokens una de otra.
VENTANA_TABLA = 40

TIPOS_SOPORTADOS = frozenset({
    "exacta", "exacta_sin_puntuacion", "fragmentos", "insiders", "editada", "tabla",
})

_REFERENCIA_FINAL = re.compile(r"(\s*\[[^\]]{0,120}\])+\s*$")
_SEPARADOR_FRAGMENTOS = re.compile(r"\[\s*\.\.\.\s*\]|\.\.\.|…")
_PALABRAS_ES = re.compile(r"\b(el|la|los|las|de|del|que|nuestros|nuestras|su|sus|con|por|para|una|un|se|en)\b", re.I)
_NUMERO = re.compile(r"\d[\d,\.\-]*\d|\d")
_SCORE = re.compile(
    r"(posicion en rango|conviccion de insiders|aceleracion de crecimiento|mejora de catalizador|total)\W*\d+\s*/\s*\d+",
    re.I,
)
_COMPRA_INSIDER = re.compile(r"\$\s*([\d,]+)\s*el\s*(\d{4}-\d{2}-\d{2})", re.I)
# Guiones tipográficos (‐ ‑ ‒ – — ― −). _normalizar_para_comparar los BORRA
# (no tienen equivalente ASCII), así que "year–over–year" se quedaba en
# "yearoveryear" y no casaba con "year-over-year". En la ruta por tokens los
# trato como separador.
_GUIONES_TIPOGRAFICOS = re.compile("[‐-―−]")


def _normalizar_para_comparar(texto: str) -> str:
    """
    Normalizo antes de comparar: minúsculas, sin acentos, espacios
    colapsados. El LLM puede citar con capitalización o espaciado
    ligeramente distinto al original sin que eso sea "inventarse" nada.

    Ojo con los apóstrofos: el recto (') sobrevive a la conversión a
    ASCII, pero el tipográfico curvo (') no tiene equivalente ASCII y
    desaparece con NFKD. Si el texto fuente usa uno y el LLM cita con
    el otro (algo frecuentísimo — "Dave & Buster's" en el 10-Q real
    suele llevar el curvo, y el LLM casi siempre escribe el recto),
    uno de los dos se queda con el apóstrofo y el otro no, y una cita
    perfectamente fiel falla la comparación por pura tipografía, no
    por contenido inventado. Elimino explícitamente todas las variantes
    de apóstrofo/comilla ANTES de la normalización Unicode, así los dos
    textos reciben el mismo tratamiento sin importar qué tipografía use
    cada uno.
    """
    for caracter in ["'", "’", "‘", '"', "“", "”"]:
        texto = texto.replace(caracter, "")
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = texto.lower()
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def _tokens(texto: str) -> list:
    """Palabras y cifras sueltas: sin puntuación ni guiones, así que
    '$70,704' pasa a ['70', '704'] y 'year-over-year' a tres tokens."""
    normalizado = _normalizar_para_comparar(_GUIONES_TIPOGRAFICOS.sub(" ", texto))
    return re.sub(r"[^a-z0-9 ]", " ", normalizado).split()


def _secuencias_numericas(texto: str) -> list:
    """Cada cifra como tupla de grupos de dígitos: 70,704 -> ('70','704').
    Descarto los enteros sueltos que parecen día/mes/año: casan con
    cualquier cosa y no demuestran nada."""
    salida = []
    for m in _NUMERO.findall(texto):
        grupos = tuple(re.findall(r"[0-9]+", m))
        if not grupos:
            continue
        if len(grupos) == 1:
            v = int(grupos[0])
            if v <= 31 or 1900 <= v <= 2100:
                continue
        salida.append(grupos)
    return salida


class _Fuente:
    """El texto contra el que verifico, indexado una sola vez por tokens
    y por trigramas para no recorrerlo entero con cada cita."""

    def __init__(self, texto: str):
        self.normalizado = _normalizar_para_comparar(texto)
        self.tokens = _tokens(texto)
        self.unido = " " + " ".join(self.tokens) + " "
        self.posiciones_token = defaultdict(list)
        for i, t in enumerate(self.tokens):
            self.posiciones_token[t].append(i)
        self.trigramas = defaultdict(list)
        t = self.tokens
        for i in range(len(t) - 2):
            self.trigramas[(t[i], t[i + 1], t[i + 2])].append(i)

    def es_literal(self, cita: str) -> bool:
        c = _normalizar_para_comparar(cita)
        return len(c) >= MIN_CARACTERES_CITA and c in self.normalizado

    def es_literal_sin_puntuacion(self, cita: str, minimo_tokens: int = 5) -> bool:
        q = _tokens(cita)
        return len(q) >= minimo_tokens and (" " + " ".join(q) + " ") in self.unido

    def son_fragmentos_exactos(self, cita: str) -> bool:
        partes = [p for p in _SEPARADOR_FRAGMENTOS.split(cita)]
        partes = [p for p in partes if len(_tokens(p)) >= 3]
        return len(partes) >= 2 and all(self.es_literal_sin_puntuacion(p, 3) for p in partes)

    def cobertura(self, cita: str) -> float:
        """Fracción de los trigramas de la cita que aparecen juntos (en una
        ventana del doble de su longitud) en algún punto de la fuente."""
        q = _tokens(cita)
        tris = [(q[i], q[i + 1], q[i + 2]) for i in range(len(q) - 2)]
        if not tris:
            return 0.0
        ventana = max(len(q) * 2, 12)
        puntos = []
        for qi, tri in enumerate(tris):
            for p in self.trigramas.get(tri, ())[:60]:
                puntos.append((p, qi))
        if not puntos:
            return 0.0
        puntos.sort()
        mejor, j = 0, 0
        cuenta = Counter()
        for p, qi in puntos:
            cuenta[qi] += 1
            while puntos[j][0] < p - ventana:
                cuenta[puntos[j][1]] -= 1
                if cuenta[puntos[j][1]] == 0:
                    del cuenta[puntos[j][1]]
                j += 1
            mejor = max(mejor, len(cuenta))
        return mejor / len(tris)

    def _posiciones(self, secuencia: tuple) -> list:
        ancla = min(secuencia, key=lambda t: len(self.posiciones_token.get(t, ())) or 10**9)
        k = secuencia.index(ancla)
        salida = []
        for p in self.posiciones_token.get(ancla, ()):
            ini = p - k
            if ini >= 0 and tuple(self.tokens[ini:ini + len(secuencia)]) == secuencia:
                salida.append(ini)
        return salida

    def cifras_en_la_misma_zona(self, cita: str) -> bool:
        """Una fila de tabla recompuesta ('Revenue 1,200 1,050') no es una
        subcadena, pero sus cifras sí están juntas en la fuente."""
        secuencias = list(dict.fromkeys(_secuencias_numericas(cita)))
        if len(secuencias) < 2:
            return False
        listas = [self._posiciones(s) for s in secuencias]
        if any(not lista for lista in listas):
            return False
        listas.sort(key=len)
        for p in listas[0]:
            if all(any(abs(p2 - p) <= VENTANA_TABLA for p2 in lista) for lista in listas[1:]):
                return True
        return False


def _es_compra_de_insiders_real(cita: str, transacciones) -> bool:
    """La lista de compras la construyo yo desde Form 4 (no está en el
    MD&A). Una cita de esa lista es fiel si cada pareja importe+fecha que
    contiene existe en las transacciones reales."""
    parejas = _COMPRA_INSIDER.findall(cita)
    if not parejas or not transacciones:
        return False
    validas = {
        (str(int(round(importe))), str(fecha))
        for _, _, fecha, importe in transacciones if importe is not None
    }
    return all((a.replace(",", ""), d) in validas for a, d in parejas)


def clasificar_cita(cita_literal: str, fuente: _Fuente, transacciones=None) -> str:
    """Devuelve el tipo de la cita (ver la tabla del docstring del módulo).
    El orden importa: de la prueba más exigente a la más laxa."""
    # Claude suele anotar la fuente al final ("[8-K, 2026-07-07]"); esa
    # coletilla no está en el documento y no es parte de la cita.
    cita = _REFERENCIA_FINAL.sub("", cita_literal or "")

    if len(_normalizar_para_comparar(cita)) < MIN_CARACTERES_CITA:
        return "demasiado_corta"
    if fuente.es_literal(cita):
        return "exacta"
    if fuente.es_literal_sin_puntuacion(cita):
        return "exacta_sin_puntuacion"
    if fuente.son_fragmentos_exactos(cita):
        return "fragmentos"
    if _es_compra_de_insiders_real(cita, transacciones):
        return "insiders"
    if _SCORE.search(_normalizar_para_comparar(cita)):
        return "score_del_sistema"
    cobertura = fuente.cobertura(cita)
    if cobertura >= COBERTURA_EDITADA:
        return "editada"
    if fuente.cifras_en_la_misma_zona(cita):
        return "tabla"
    if len(_PALABRAS_ES.findall(cita)) >= 3:
        return "traducida"
    if cobertura >= COBERTURA_PARAFRASIS:
        return "parafrasis"
    return "sin_soporte"


def verificar_citas(citas: list, texto_fuente: str, transacciones=None) -> dict:
    """
    Compruebo, de forma determinista (sin otro LLM), si cada cita que el
    modelo dice haber sacado de los documentos existe de verdad ahí. No
    confío en que el modelo se autoevalúe: comparo contra el texto real.

    transacciones: las compras de insiders que vio el modelo, como tuplas
    (nombre, cargo, fecha, importe), para validar las citas de esa lista.
    """
    fuente = _Fuente(texto_fuente)

    detalle = []
    exactas = 0
    soportadas = 0
    for cita in citas:
        tipo = clasificar_cita(cita.get("cita_literal", ""), fuente, transacciones)
        es_exacta = tipo == "exacta"
        es_soportada = tipo in TIPOS_SOPORTADOS
        exactas += es_exacta
        soportadas += es_soportada
        detalle.append({**cita, "verificada": es_exacta, "soportada": es_soportada, "tipo": tipo})

    total = len(citas)
    pct_exactas = (exactas / total * 100) if total > 0 else 0
    pct_soportadas = (soportadas / total * 100) if total > 0 else 0

    return {
        "version_verificador": VERSION_VERIFICADOR,
        "citas_totales": total,
        # Estricto: igual que la v1, para comparar épocas.
        "citas_verificadas": exactas,
        "porcentaje_verificado": round(pct_exactas, 1),
        # El que decide la etiqueta.
        "citas_soportadas": soportadas,
        "porcentaje_soportado": round(pct_soportadas, 1),
        "alucinacion_detectada": total > 0 and pct_soportadas < UMBRAL_ALUCINACION,
        "citas_debiles": total > 0 and UMBRAL_ALUCINACION <= pct_soportadas < UMBRAL_CITAS_DEBILES,
        "detalle": detalle,
    }
