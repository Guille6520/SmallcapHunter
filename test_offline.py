"""
test_offline.py — suite de pruebas que NO necesita base de datos,
API keys ni internet. Prueba toda la lógica pura del proyecto:
normalización, detección de clusters, filtro C-suite, scoring,
verificador de citas, extracción XBRL, regex del MD&A y extracción
de items del 8-K.

La uso como smoke test antes de tocar nada: si esto no pasa,
no tiene sentido ejecutar el pipeline completo.

Cómo ejecutarla:
  python test_offline.py

Si todo va bien termina con "TODOS LOS TESTS PASAN". Si algo falla,
el assert dice exactamente qué y con qué valores.
"""

from datetime import date

fallos = []

def check(nombre, condicion, detalle=""):
    if condicion:
        print(f"  OK   {nombre}")
    else:
        print(f"  FALLO {nombre} {detalle}")
        fallos.append(nombre)


# ---------- normalizar.py ----------
print("\n[normalizar.py]")
from normalizar import (
    normalizar_cik, normalizar_fecha, validar_fecha_transaccion,
    normalizar_precio, normalizar_acciones, calcular_importe, normalizar_codigo,
)

check("CIK float de pandas", normalizar_cik("320193.0") == "0000320193")
check("CIK normal", normalizar_cik("320193") == "0000320193")
check("CIK corrupto (>10 digitos)", normalizar_cik("12345678901") is None)
check("fecha formato SEC", normalizar_fecha("15-MAR-2023") == date(2023, 3, 15))
check("fecha ISO", normalizar_fecha("2023-03-15") == date(2023, 3, 15))
check("fecha 'NaN' textual", normalizar_fecha("NaN") is None)
check("tx posterior al filing descartada",
      validar_fecha_transaccion(date(2023, 5, 1), date(2023, 4, 1)) is None)
check("tx futura descartada", validar_fecha_transaccion(date(2099, 1, 1)) is None)
check("precio 0 -> None (no es compra de mercado)", normalizar_precio(0) is None)
check("precio NaN -> None", normalizar_precio(float("nan")) is None)
check("precio absurdo (>1M) -> None", normalizar_precio(5_000_000) is None)
check("acciones negativas -> valor absoluto", normalizar_acciones(-100) == 100)
check("importe sin precio -> None (no 0 falso)", calcular_importe(100, None) is None)
check("codigo X valido (lo aprendimos por las malas)", normalizar_codigo("x") == "X")
check("codigo basura -> None", normalizar_codigo("QQ") is None)


# ---------- filtro_capa1.py ----------
print("\n[filtro_capa1.py]")
from filtro_capa1 import (
    bolsa_es_valida, es_cargo_csuite, detectar_cluster_buying, hay_csuite_en_ventana,
)

check("NASDAQ valida", bolsa_es_valida("NASDAQ Global Select"))
check("'OTC Markets - NASDAQ Referenced' descartada (el falso positivo clasico)",
      not bolsa_es_valida("OTC Markets - NASDAQ Referenced"))
check("Pink sheets descartada", not bolsa_es_valida("PINK Current"))

check("CEO es C-suite", es_cargo_csuite("Chief Executive Officer"))
check("President & CEO es C-suite", es_cargo_csuite("President & CEO"))
check("EVP and CFO es C-suite (el CFO manda sobre el EVP)",
      es_cargo_csuite("EVP and Chief Financial Officer"))
check("Vice President NO es C-suite (el bug del substring)",
      not es_cargo_csuite("Vice President of Sales"))
check("SVP NO es C-suite", not es_cargo_csuite("SVP, Finance"))
check("Vice Chairman NO es C-suite", not es_cargo_csuite("Vice Chairman"))
check("Director NO es C-suite", not es_cargo_csuite("Director"))

# 3 insiders en 40 dias: cumple, y la fecha es la compra del tercero
tx = [(date(2024, 1, 1), "A"), (date(2024, 1, 20), "B"), (date(2024, 2, 10), "C")]
r = detectar_cluster_buying(tx, 60, 3)
check("cluster compacto detectado", r["cumple"])
check("fecha_deteccion = compra del 3er insider (no fin de ventana)",
      r["fecha_deteccion"] == date(2024, 2, 10), f"-> {r['fecha_deteccion']}")

# Las mismas 3 personas repartidas en 3 anios: NO cumple
tx2 = [(date(2020, 1, 1), "A"), (date(2021, 6, 1), "B"), (date(2023, 1, 1), "C")]
check("3 compras en 3 anios NO es cluster", not detectar_cluster_buying(tx2, 60, 3)["cumple"])

# El mismo insider comprando 3 veces: NO cumple (personas distintas, no compras)
tx3 = [(date(2024, 1, 1), "A"), (date(2024, 1, 5), "A"), (date(2024, 1, 9), "A")]
check("1 insider x3 compras NO es cluster", not detectar_cluster_buying(tx3, 60, 3)["cumple"])

# C-suite fuera de la ventana del cluster no valida (el bug corregido)
cargos = [(date(2016, 3, 1), "Chief Executive Officer"), (date(2024, 1, 20), "Director")]
check("CEO de hace 8 anios NO valida el cluster de hoy",
      not hay_csuite_en_ventana(cargos, date(2024, 2, 10), 60))
check("CFO dentro de la ventana SI valida",
      hay_csuite_en_ventana([(date(2024, 1, 20), "Chief Financial Officer")], date(2024, 2, 10), 60))


# ---------- detective.py: verificador de citas ----------
print("\n[detective.py — verificador de citas]")
from detective import verificar_citas, _normalizar_para_comparar

fuente = "The company’s revenue increased 45% during the quarter due to new store openings."
citas_ok = [{"afirmacion": "crece", "cita_literal": "the company's revenue increased 45%"}]
v = verificar_citas(citas_ok, fuente)
check("cita fiel con apostrofo recto vs curvo -> verificada", v["citas_verificadas"] == 1)

citas_mal = [
    {"afirmacion": "inventada", "cita_literal": "revenue will triple next year guaranteed"},
    {"afirmacion": "inventada2", "cita_literal": "the CEO announced a merger with Apple"},
]
v2 = verificar_citas(citas_mal, fuente)
check("citas inventadas -> alucinacion detectada", v2["alucinacion_detectada"])

mezcla = citas_ok + citas_mal  # 1 de 3 = 33% < 50%
check("1/3 verificadas -> alucinacion", verificar_citas(mezcla, fuente)["alucinacion_detectada"])

# Claude anota la fuente al final de la cita ("[8-K, 2026-07-07]") —
# el verificador debe recortar esa coletilla, no castigar la cita
cita_anotada = [{"afirmacion": "x",
                 "cita_literal": "revenue increased 45% during the quarter [MD&A, Q1 2026]"}]
check("cita fiel con referencia entre corchetes al final -> verificada",
      verificar_citas(cita_anotada, fuente)["citas_verificadas"] == 1)
check("normalizacion colapsa espacios y mayusculas",
      _normalizar_para_comparar("  HOLA   Mundo ") == "hola mundo")


# ---------- scorer_capa2.py ----------
print("\n[scorer_capa2.py]")
import scorer_capa2
from scorer_capa2 import score_precio, score_temporal, score_catalizador, score_conviccion

check("cerca de minimos 52w -> 10", score_precio(0.10) == 10)
check("cerca de maximos 52w -> 0", score_precio(0.95) == 0)
check("sin dato -> 0", score_precio(None) == 0)

# Para score_temporal y score_catalizador simulo la base de datos con
# un cursor falso: una cola de resultados que se consumen en orden.
class FakeCursor:
    def __init__(self, cola):
        self.cola = cola
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self.cola.pop(0)
    def fetchone(self):
        return self.cola.pop(0)
    def close(self):
        pass

class FakeConn:
    def __init__(self, cola):
        self.cola = cola
    def cursor(self):
        return FakeCursor(self.cola)

# Serie que ACELERA (la del ejemplo de la documentacion): 100,105,116,135,170,230
serie_acelera = [(2024, 1, 100), (2024, 2, 105), (2024, 3, 116), (2024, 4, 135),
                 (2025, 1, 170), (2025, 2, 230)]
st_ = score_temporal(FakeConn([serie_acelera]), 1)
check("serie que acelera puntua alto (>=8)", st_ >= 8, f"-> {st_}")

# Crecimiento lineal en valor absoluto = tasa que DESACELERA: puntua poco
serie_lineal = [(2024, 1, 100), (2024, 2, 110), (2024, 3, 120), (2024, 4, 130),
                (2025, 1, 140), (2025, 2, 150)]
st_lineal = score_temporal(FakeConn([serie_lineal]), 1)
check("crecimiento lineal puntua bajo (<=4)", st_lineal <= 4, f"-> {st_lineal}")

# Hueco de trimestres: 2023Q4 -> 2024Q3 no son adyacentes, ese par se salta
serie_hueco = [(2023, 3, 100), (2023, 4, 500), (2024, 3, 120), (2024, 4, 130), (2025, 1, 140)]
st_hueco = score_temporal(FakeConn([serie_hueco]), 1)
check("trimestres no adyacentes no generan tasa QoQ falsa", isinstance(st_hueco, int))

# score_catalizador: margen mejorando (+5) y caja mejorando (+5)
margenes = [(0.30,), (0.31,), (0.35,), (0.38,)]           # query de margenes
fcf = [(2024, 1, -900), (2024, 2, -600), (2024, 3, -300), (2024, 4, -50)]  # via _serie_trimestral
scat = score_catalizador(FakeConn([margenes, fcf]), 1)
check("margen y caja mejorando -> 10", scat == 10, f"-> {scat}")

# score_conviccion: solo cuenta la ventana del cluster, no todo el historico.
# 3 insiders en enero-2024 (cluster) + 3 insiders distintos repartidos 2018-2020.
compras = [
    (date(2018, 1, 1), "Viejo1", 100_000),
    (date(2019, 1, 1), "Viejo2", 100_000),
    (date(2020, 1, 1), "Viejo3", 100_000),
    (date(2024, 1, 5), "A", 100_000),
    (date(2024, 1, 15), "B", 100_000),
    (date(2024, 1, 25), "C", 100_000),
]
sc = score_conviccion(FakeConn([compras]), 1, {"dias_ventana_cluster": "60", "min_insiders_cluster": "3"})
# amplitud de 3 insiders (no 6) = 3; intensidad de 300k = 3 -> 6
check("conviccion cuenta el cluster (3 insiders), no el historico (6)", sc == 6, f"-> {sc}")


# ---------- enriquecedor_xbrl.py ----------
print("\n[enriquecedor_xbrl.py]")
from enriquecedor_xbrl import extraer_serie_xbrl, _es_trimestral, XBRL_CAMPOS

check("periodo de 91 dias es trimestral",
      _es_trimestral({"start": "2024-01-01", "end": "2024-04-01"}))
check("periodo de 181 dias (acumulado) NO es trimestral",
      not _es_trimestral({"start": "2024-01-01", "end": "2024-06-30"}))
check("foto puntual (sin start) no es flujo trimestral",
      not _es_trimestral({"end": "2024-06-30"}))

# El caso ASC 606: la empresa reporta con SalesRevenueNet hasta 2017 y
# cambia de etiqueta en 2018. La fusion debe cubrir AMBOS tramos, y el
# acumulado de 6 meses debe quedar filtrado.
facts = {"facts": {"us-gaap": {
    "SalesRevenueNet": {"units": {"USD": [
        {"start": "2017-01-01", "end": "2017-03-31", "val": 100, "form": "10-Q", "fy": 2017, "fp": "Q1"},
        {"start": "2017-01-01", "end": "2017-06-30", "val": 210, "form": "10-Q", "fy": 2017, "fp": "Q2"},
        {"start": "2017-04-01", "end": "2017-06-30", "val": 110, "form": "10-Q", "fy": 2017, "fp": "Q2"},
    ]}},
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        {"start": "2018-01-01", "end": "2018-03-31", "val": 130, "form": "10-Q", "fy": 2018, "fp": "Q1"},
    ]}},
}}}
serie = extraer_serie_xbrl(facts, XBRL_CAMPOS["revenue"], es_flujo=True)
check("fusion de etiquetas cubre el tramo antiguo Y el nuevo (ASC 606)",
      set(serie.keys()) == {"2017-03-31", "2017-06-30", "2018-03-31"},
      f"-> {sorted(serie.keys())}")
check("el acumulado de 6 meses quedo filtrado (Q2 = 110, no 210)",
      serie["2017-06-30"]["val"] == 110)


# ---------- ingesta_10q.py: regex del MD&A ----------
print("\n[ingesta_10q.py — regex del Item 2]")
from ingesta_10q import (
    PATRON_INICIO_MDA, PATRON_INICIO_MDA_LAXO, PATRON_FIN_MDA,
    PATRON_FIN_MDA_ALT, _es_mencion_cruzada,
)

check("apostrofo curvo (el de los filings reales)",
      PATRON_INICIO_MDA.search("Item 2. Management’s Discussion and Analysis") is not None)
check("apostrofo recto", PATRON_INICIO_MDA.search("Item 2. Management's Discussion and Analysis") is not None)
check("sin posesivo y en mayusculas",
      PATRON_INICIO_MDA.search("ITEM 2 MANAGEMENTS DISCUSSION AND ANALYSIS") is not None)
check("dos puntos en vez de punto tras el numero de item",
      PATRON_INICIO_MDA.search("Item 2: Management's Discussion and Analysis") is not None)
check("patron de fin (Item 3)",
      PATRON_FIN_MDA.search("Item 3. Quantitative and Qualitative Disclosures") is not None)
check("patron de fin alternativo (Item 4, cuando no hay Item 3 con ese titulo)",
      PATRON_FIN_MDA_ALT.search("Item 4. Controls and Procedures") is not None)

# Caso real BYRN: el patron laxo (sin "Item 2" delante) engancho la
# frase de forward-looking-statements que CITA el titulo del MD&A entre
# comillas — eso es legitimo, es el arranque real de la seccion.
check("patron laxo encuentra el titulo sin 'Item 2' delante",
      PATRON_INICIO_MDA_LAXO.search(
          "our Management's Discussion and Analysis of Financial Condition "
          "and results, are forward-looking statements") is not None)

# Caso real BYRN (el que rompia antes del filtro): una MENCION cruzada
# en las notas del Item 1, remitiendo al 10-K anual — no es el
# encabezado real y debe descartarse.
texto_mencion = (
    "the financial statements should be read in conjunction with "
    "Management's Discussion and Analysis of Financial Condition and "
    "Results of Operations contained in the Company's annual report "
    "on Form 10-K"
)
match_mencion = PATRON_INICIO_MDA_LAXO.search(texto_mencion)
check("la mencion cruzada SI casa con el patron laxo (por eso hace falta el filtro)",
      match_mencion is not None)
check("pero el filtro de mencion cruzada la detecta y la descarta",
      match_mencion and _es_mencion_cruzada(texto_mencion, match_mencion.start()))

texto_encabezado_real = (
    "PART I ITEM 2. Management's Discussion and Analysis of Financial "
    "Condition and Results of Operations. Overview: our revenue grew"
)
match_real = PATRON_INICIO_MDA.search(texto_encabezado_real)
check("un encabezado real (via patron principal) no se marca como mencion cruzada",
      match_real and not _es_mencion_cruzada(texto_encabezado_real, match_real.start()))


# ---------- auditor.py: el prompt no debe sesgar hacia EMPEORA ----------
print("\n[auditor.py — simetria del prompt]")
from auditor import construir_prompt_auditor

# Historico real: 26/34 auditorias EMPEORAN al Detective y solo 1 MEJORA.
# La causa era que el prompt solo preguntaba por generosidad excesiva y
# riesgos omitidos, nunca por fortalezas infravaloradas — este test
# congela la correccion para que no se pierda en un refactor futuro.
contexto_fake = {
    "nombre": "Empresa Ficticia", "ticker": "FAKE",
    "texto_mda": "El negocio crecio este trimestre.",
    "eventos_8k": [], "activistas": [], "senales_mercado": None,
}
respuesta_detective_fake = {
    "catalizador_no_obvio": "x", "tesis_inversion": "y",
    "riesgos": [], "veredicto_preliminar": "INTERESANTE",
}
prompt_auditor = construir_prompt_auditor(contexto_fake, respuesta_detective_fake)

check("pregunta por riesgos omitidos (direccion EMPEORA)",
      "riesgo" in prompt_auditor.lower() and "no mencion" in prompt_auditor.lower())
check("tambien pregunta por fortalezas infravaloradas (direccion MEJORA)",
      "fortaleza" in prompt_auditor.lower() or "infravalor" in prompt_auditor.lower())
check("el JSON de salida tiene un campo simetrico para fortalezas",
      '"fortalezas_omitidas_por_detective"' in prompt_auditor)
check("tambien admite que la interpretacion fuera demasiado conservadora",
      "conservador" in prompt_auditor.lower())


# ---------- ingesta_8k.py: items y extraccion de secciones ----------
print("\n[ingesta_8k.py — items y extraccion de secciones]")
from ingesta_8k import parsear_items, construir_patron_item, extraer_item_8k

check("items con espacios", parsear_items("1.01, 9.01") == ["1.01", "9.01"])
check("items vacio -> lista vacia", parsear_items("") == [])
check("items None -> lista vacia", parsear_items(None) == [])

check("patron encuentra el item",
      construir_patron_item("1.01").search(
          "Item 1.01 Entry into a Material Definitive Agreement") is not None)
check("el punto es literal, no comodin (1.01 no casa con 1x01)",
      construir_patron_item("1.01").search("Item 1x01 cualquier cosa") is None)
check("mayusculas del filing real",
      construir_patron_item("5.02").search("ITEM 5.02. DEPARTURE OF DIRECTORS") is not None)

# Documento sintetico con la estructura tipica de un 8-K real: el item
# aparece DOS veces (mencion corta de portada/indice y cuerpo real con
# contenido), luego otro item y el bloque de firmas.
doc_8k = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\n"
    "FORM 8-K\n"
    "Item 1.01\n"
    "Item 9.01\n"
    "\n"
    "Item 1.01 Entry into a Material Definitive Agreement.\n"
    + "On July 1, 2026, the Company entered into a supply agreement "
      "with a major customer for its new product line. " * 15
    + "\nItem 9.01 Financial Statements and Exhibits.\n"
    "(d) Exhibits\n"
    "SIGNATURES\n"
    "Pursuant to the requirements of the Securities Exchange Act of 1934..."
)
texto_8k, aislado_8k = extraer_item_8k(doc_8k, "1.01")
check("elige el cuerpo real, no la mencion de portada (hueco mas grande)",
      aislado_8k and "supply agreement" in texto_8k)
check("corta en el siguiente item (no arrastra los exhibits)",
      "Exhibits" not in texto_8k)

# El ultimo item del documento no tiene otro "Item" despues — la
# frontera tiene que ser el bloque de firmas
doc_ultimo = (
    "Item 5.02 Departure of Directors or Certain Officers.\n"
    + "On July 2, 2026, the Chief Financial Officer notified the "
      "Company of his resignation effective July 15, 2026. " * 10
    + "\nSIGNATURES\nPursuant to the requirements of the Securities "
    "Exchange Act of 1934, the registrant has duly caused this report..."
)
texto_ult, aislado_ult = extraer_item_8k(doc_ultimo, "5.02")
check("ultimo item del doc: la frontera es el bloque de firmas",
      aislado_ult and "resignation" in texto_ult)
check("no arrastra el bloque de firmas", "duly caused" not in texto_ult)

# Item que el indice declaro pero no aparece en el cuerpo -> fallback
# documentado, igual que el fallback del MD&A
_, aislado_ausente = extraer_item_8k(doc_8k, "5.02")
check("item ausente -> fallback (seccion_aislada=False)", not aislado_ausente)

# La fuente de verificacion de citas se amplia con los eventos: una
# cita fiel a un 8-K no puede marcarse como alucinacion solo por no
# estar en el MD&A
from detective import texto_fuente_citas
ctx_falso = {
    "texto_mda": "Revenue increased due to strong demand.",
    "eventos_8k": [("1.01", date(2026, 7, 1),
                    "the Company entered into a definitive agreement with ACME Corp")],
}
v8k = verificar_citas(
    [{"afirmacion": "contrato", "cita_literal": "definitive agreement with ACME Corp"}],
    texto_fuente_citas(ctx_falso),
)
check("cita del 8-K verificada contra la fuente ampliada", v8k["citas_verificadas"] == 1)


# ---------- ingesta_13dg.py: porcentaje y deteccion de shelf ----------
print("\n[ingesta_13dg.py — porcentaje declarado y shelf]")
from ingesta_13dg import extraer_pct_participacion, es_formulario_shelf

# Portada tipica de un Schedule 13G real
texto_13g = (
    "CUSIP No. 04271T100\n"
    "11. Aggregate Amount Beneficially Owned by Each Reporting Person: 2,145,678\n"
    "13. Percent of Class Represented by Amount in Row (11): 9.9%\n"
    "14. Type of Reporting Person: IA\n"
)
check("extrae el 9.9% de la portada del schedule",
      extraer_pct_participacion(texto_13g) == 9.9)
check("texto sin porcentaje -> None",
      extraer_pct_participacion("no hay nada que extraer aqui") is None)
check("None -> None (sin explotar)", extraer_pct_participacion(None) is None)
# El regex exige la frase "percent of class" cerca — un numero suelto
# con % en otro contexto no debe colar como participacion
check("un % suelto sin contexto no cuela",
      extraer_pct_participacion("the interest rate is 12.5% per annum") is None)

# Formato XML nuevo (SCHEDULE 13G desde finales de 2024)
check("extrae el porcentaje de la etiqueta XML nueva",
      extraer_pct_participacion("<percentOfClass>9.9</percentOfClass>") == 9.9)

check("S-3 es shelf", es_formulario_shelf("S-3"))
check("S-3/A es shelf", es_formulario_shelf("S-3/A"))
check("424B5 es shelf", es_formulario_shelf("424B5"))
check("8-K NO es shelf", not es_formulario_shelf("8-K"))
check("None NO es shelf (sin explotar)", not es_formulario_shelf(None))


# ---------- notificador_telegram.py ----------
print("\n[notificador_telegram.py]")
import os
token_previo = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
chat_previo = os.environ.pop("TELEGRAM_CHAT_ID", None)
from notificador_telegram import enviar_telegram
check("sin configurar devuelve False sin lanzar excepcion",
      enviar_telegram("prueba") is False)
if token_previo:
    os.environ["TELEGRAM_BOT_TOKEN"] = token_previo
if chat_previo:
    os.environ["TELEGRAM_CHAT_ID"] = chat_previo


# ---------- resumen ----------
print("\n" + "=" * 50)
if fallos:
    print(f"FALLAN {len(fallos)} TESTS: {fallos}")
    raise SystemExit(1)
print("TODOS LOS TESTS PASAN")

