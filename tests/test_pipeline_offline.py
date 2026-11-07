"""
tests/test_pipeline_offline.py — la lógica pura del pipeline, migrada a
pytest desde el antiguo test_offline.py (raíz del repo, ahora retirado).

Mismo contenido, mismos casos límite reales que motivaron cada test
(el bug del substring en "Vice President", el sesgo EMPEORA del
Auditor, el caso real BYRN del regex del MD&A) — solo cambia el
runner: pytest en vez de un `check()` casero con prints. No necesita
base de datos, API keys ni internet; eso es justo lo que la suite de
CI ejecuta en cada push (ver .github/workflows/tests.yml).
"""

from datetime import date

import pytest


# ---------- normalizar.py ----------

def test_normalizar():
    from normalizar import (
        normalizar_cik, normalizar_fecha, validar_fecha_transaccion,
        normalizar_precio, normalizar_acciones, calcular_importe, normalizar_codigo,
    )

    assert normalizar_cik("320193.0") == "0000320193", "CIK float de pandas"
    assert normalizar_cik("320193") == "0000320193", "CIK normal"
    assert normalizar_cik("12345678901") is None, "CIK corrupto (>10 digitos)"
    assert normalizar_fecha("15-MAR-2023") == date(2023, 3, 15), "fecha formato SEC"
    assert normalizar_fecha("2023-03-15") == date(2023, 3, 15), "fecha ISO"
    assert normalizar_fecha("NaN") is None, "fecha 'NaN' textual"
    assert validar_fecha_transaccion(date(2023, 5, 1), date(2023, 4, 1)) is None, \
        "tx posterior al filing descartada"
    assert validar_fecha_transaccion(date(2099, 1, 1)) is None, "tx futura descartada"
    assert normalizar_precio(0) is None, "precio 0 -> None (no es compra de mercado)"
    assert normalizar_precio(float("nan")) is None, "precio NaN -> None"
    assert normalizar_precio(5_000_000) is None, "precio absurdo (>1M) -> None"
    assert normalizar_acciones(-100) == 100, "acciones negativas -> valor absoluto"
    assert calcular_importe(100, None) is None, "importe sin precio -> None (no 0 falso)"
    assert normalizar_codigo("x") == "X", "codigo X valido (lo aprendimos por las malas)"
    assert normalizar_codigo("QQ") is None, "codigo basura -> None"


# ---------- filtro_capa1.py ----------

def test_filtro_capa1_bolsa_y_cargos():
    from filtro_capa1 import bolsa_es_valida, es_cargo_csuite

    assert bolsa_es_valida("NASDAQ Global Select")
    assert not bolsa_es_valida("OTC Markets - NASDAQ Referenced"), \
        "el falso positivo clasico"
    assert not bolsa_es_valida("PINK Current")

    assert es_cargo_csuite("Chief Executive Officer")
    assert es_cargo_csuite("President & CEO")
    assert es_cargo_csuite("EVP and Chief Financial Officer"), \
        "el CFO manda sobre el EVP"
    assert not es_cargo_csuite("Vice President of Sales"), "el bug del substring"
    assert not es_cargo_csuite("SVP, Finance")
    assert not es_cargo_csuite("Vice Chairman")
    assert not es_cargo_csuite("Director")


def test_filtro_capa1_cluster_buying():
    from filtro_capa1 import detectar_cluster_buying, hay_csuite_en_ventana

    # 3 insiders en 40 dias: cumple, y la fecha es la compra del tercero
    tx = [(date(2024, 1, 1), "A"), (date(2024, 1, 20), "B"), (date(2024, 2, 10), "C")]
    r = detectar_cluster_buying(tx, 60, 3)
    assert r["cumple"], "cluster compacto detectado"
    assert r["fecha_deteccion"] == date(2024, 2, 10), \
        "fecha_deteccion = compra del 3er insider (no fin de ventana)"

    # Las mismas 3 personas repartidas en 3 anios: NO cumple
    tx2 = [(date(2020, 1, 1), "A"), (date(2021, 6, 1), "B"), (date(2023, 1, 1), "C")]
    assert not detectar_cluster_buying(tx2, 60, 3)["cumple"], "3 compras en 3 anios NO es cluster"

    # El mismo insider comprando 3 veces: NO cumple (personas distintas, no compras)
    tx3 = [(date(2024, 1, 1), "A"), (date(2024, 1, 5), "A"), (date(2024, 1, 9), "A")]
    assert not detectar_cluster_buying(tx3, 60, 3)["cumple"], "1 insider x3 compras NO es cluster"

    # C-suite fuera de la ventana del cluster no valida (el bug corregido)
    cargos = [(date(2016, 3, 1), "Chief Executive Officer"), (date(2024, 1, 20), "Director")]
    assert not hay_csuite_en_ventana(cargos, date(2024, 2, 10), 60), \
        "CEO de hace 8 anios NO valida el cluster de hoy"
    assert hay_csuite_en_ventana(
        [(date(2024, 1, 20), "Chief Financial Officer")], date(2024, 2, 10), 60
    ), "CFO dentro de la ventana SI valida"


# ---------- detective.py: verificador de citas ----------

def test_verificador_citas():
    from detective import verificar_citas, _normalizar_para_comparar

    fuente = "The company's revenue increased 45% during the quarter due to new store openings."
    citas_ok = [{"afirmacion": "crece", "cita_literal": "the company's revenue increased 45%"}]
    v = verificar_citas(citas_ok, fuente)
    assert v["citas_verificadas"] == 1, "cita fiel con apostrofo recto vs curvo -> verificada"

    citas_mal = [
        {"afirmacion": "inventada", "cita_literal": "revenue will triple next year guaranteed"},
        {"afirmacion": "inventada2", "cita_literal": "the CEO announced a merger with Apple"},
    ]
    v2 = verificar_citas(citas_mal, fuente)
    assert v2["alucinacion_detectada"], "citas inventadas -> alucinacion detectada"

    mezcla = citas_ok + citas_mal  # 1 de 3 = 33% < 50%
    assert verificar_citas(mezcla, fuente)["alucinacion_detectada"], "1/3 verificadas -> alucinacion"

    # Claude anota la fuente al final de la cita ("[8-K, 2026-07-07]") —
    # el verificador debe recortar esa coletilla, no castigar la cita
    cita_anotada = [{"afirmacion": "x",
                     "cita_literal": "revenue increased 45% during the quarter [MD&A, Q1 2026]"}]
    assert verificar_citas(cita_anotada, fuente)["citas_verificadas"] == 1, \
        "cita fiel con referencia entre corchetes al final -> verificada"
    assert _normalizar_para_comparar("  HOLA   Mundo ") == "hola mundo"


# ---------- scorer_capa2.py ----------

class _FakeCursor:
    """Simula un cursor de psycopg2 con una cola de resultados que se
    consumen en orden — suficiente para probar la lógica de scoring sin
    una base de datos real."""

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


class _FakeConn:
    def __init__(self, cola):
        self.cola = cola

    def cursor(self):
        return _FakeCursor(self.cola)


def test_score_precio():
    from scorer_capa2 import score_precio

    assert score_precio(0.10) == 10, "cerca de minimos 52w -> 10"
    assert score_precio(0.95) == 0, "cerca de maximos 52w -> 0"
    assert score_precio(None) == 0, "sin dato -> 0"


def _cierres(anio, trimestre, n):
    """n fechas de cierre de trimestre seguidas, desde el trimestre calendario dado."""
    fechas, a, t = [], anio, trimestre
    for _ in range(n):
        mes = t * 3
        dia = 31 if mes in (3, 12) else 30
        fechas.append(date(a, mes, dia))
        a, t = (a + 1, 1) if t == 4 else (a, t + 1)
    return fechas


def _serie(valores, anio=2024, trimestre=1):
    """Serie [(fecha_de_cierre, valor)] con un cierre por trimestre, sin huecos."""
    return list(zip(_cierres(anio, trimestre, len(valores)), valores))


def test_score_temporal():
    from scorer_capa2 import score_temporal

    # Serie que ACELERA (la del ejemplo de la documentacion)
    serie_acelera = _serie([100, 105, 116, 135, 170, 230])
    st_ = score_temporal(_FakeConn([serie_acelera]), 1)
    assert st_ >= 8, f"serie que acelera puntua alto (>=8) -> {st_}"

    # Crecimiento lineal en valor absoluto = tasa que DESACELERA: puntua poco
    serie_lineal = _serie([100, 110, 120, 130, 140, 150])
    st_lineal = score_temporal(_FakeConn([serie_lineal]), 1)
    assert st_lineal <= 4, f"crecimiento lineal puntua bajo (<=4) -> {st_lineal}"


def test_un_hueco_de_trimestres_no_genera_una_tasa_trimestral_falsa():
    """Antes el test solo comprobaba que el resultado fuera un entero. Aqui el hueco (falta un trimestre
    entre 2024Q1 y 2024Q3) esconde un salto de 120 a 400. Si el par se empalmara, ese salto contaria como
    la tasa de UN trimestre y la serie parecería acelerar (puntuaria 10); saltandolo, lo que queda es
    crecimiento que se frena (puntua bajo)."""
    from scorer_capa2 import score_temporal

    cierres = _cierres(2023, 3, 3) + _cierres(2024, 3, 3)           # 2023Q3, Q4, 2024Q1  ... hueco ...  2024Q3, Q4, 2025Q1
    con_hueco = list(zip(cierres, [100, 110, 120, 400, 410, 420]))

    assert score_temporal(_FakeConn([con_hueco]), 1) <= 4,         "el salto 120 -> 400 cae en el hueco: no puede contarse como una tasa trimestral"


def test_trimestres_con_etiquetas_de_ano_engañosas_pero_cierres_lejanos_no_se_empalman():
    """El caso real de BFS, TGEN, KLTR y otras 75 empresas: el cierre del 30-jun-2024 figuraba como
    '2025 Q2' y el del 30-sep-2025 como '2025 Q3', es decir, 'consecutivos' con 457 dias entre
    medias. El score decide la adyacencia con la fecha de cierre y no con la etiqueta, asi que esos
    dos NO forman un par."""
    from scorer_capa2 import _son_consecutivos, score_temporal

    assert _son_consecutivos(date(2024, 6, 30), date(2024, 9, 30)) is True
    assert _son_consecutivos(date(2024, 6, 30), date(2025, 9, 30)) is False, "15 meses no es un trimestre"
    assert _son_consecutivos(date(2025, 3, 31), date(2025, 6, 30)) is True
    # los calendarios de 52/53 semanas mueven el cierre unos dias
    assert _son_consecutivos(date(2024, 12, 28), date(2025, 3, 29)) is True
    assert _son_consecutivos(date(2024, 12, 28), date(2025, 6, 28)) is False, "dos trimestres de distancia"

    # Solo 3 pares seguidos de verdad (2024Q3->Q4, Q4->2025Q1, 2025Q1->Q2): la serie con el salto de 15
    # meses en medio tiene 3 tasas validas, no 4, y el salto 120 -> 600 no cuenta.
    f = [date(2024, 3, 31), date(2024, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    serie = list(zip(f, [100, 110, 600, 610, 620, 630]))
    assert score_temporal(_FakeConn([serie]), 1) <= 4, "el salto 110 -> 600 (15 meses) no puede contarse como crecimiento trimestral"


def test_la_serie_se_pide_ordenada_por_fecha_de_cierre_y_no_por_etiqueta():
    """Las etiquetas de ano/trimestre pueden estar desordenadas respecto a la fecha real."""
    from scorer_capa2 import _serie_trimestral

    consultas = []

    class Cur:
        def execute(self, sql, params=None):
            consultas.append(sql)

        def fetchall(self):
            return []

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

    _serie_trimestral(Conn(), 1, "revenue")

    sql = consultas[0]
    assert "select fecha_fin, revenue" in sql and "order by fecha_fin" in sql
    assert "anio_fiscal" not in sql and "trimestre" not in sql.replace("metricas_trimestrales", "")


def test_score_catalizador():
    from scorer_capa2 import score_catalizador

    margenes = [(0.30,), (0.31,), (0.35,), (0.38,)]
    fcf = _serie([-900, -600, -300, -50])
    scat = score_catalizador(_FakeConn([margenes, fcf]), 1)
    assert scat == 10, f"margen y caja mejorando -> 10, dio {scat}"


def test_score_conviccion():
    from scorer_capa2 import score_conviccion

    # Solo cuenta la ventana del cluster, no todo el historico: 3
    # insiders en enero-2024 (cluster) + 3 insiders distintos 2018-2020.
    compras = [
        (date(2018, 1, 1), "Viejo1", 100_000),
        (date(2019, 1, 1), "Viejo2", 100_000),
        (date(2020, 1, 1), "Viejo3", 100_000),
        (date(2024, 1, 5), "A", 100_000),
        (date(2024, 1, 15), "B", 100_000),
        (date(2024, 1, 25), "C", 100_000),
    ]
    sc = score_conviccion(
        _FakeConn([compras]), 1, {"dias_ventana_cluster": "60", "min_insiders_cluster": "3"}
    )
    assert sc == 6, f"conviccion cuenta el cluster (3 insiders), no el historico (6) -> {sc}"


# ---------- enriquecedor_xbrl.py ----------

def test_enriquecedor_xbrl():
    from enriquecedor_xbrl import extraer_serie_xbrl, _es_trimestral, XBRL_CAMPOS

    assert _es_trimestral({"start": "2024-01-01", "end": "2024-04-01"}), "periodo de 91 dias es trimestral"
    assert not _es_trimestral({"start": "2024-01-01", "end": "2024-06-30"}), \
        "periodo de 181 dias (acumulado) NO es trimestral"
    assert not _es_trimestral({"end": "2024-06-30"}), "foto puntual (sin start) no es flujo trimestral"

    # Caso ASC 606: la empresa reporta con SalesRevenueNet hasta 2017 y
    # cambia de etiqueta en 2018. La fusion debe cubrir AMBOS tramos, y
    # el acumulado de 6 meses debe quedar filtrado.
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
    assert set(serie.keys()) == {"2017-03-31", "2017-06-30", "2018-03-31"}, \
        f"fusion de etiquetas cubre el tramo antiguo Y el nuevo (ASC 606) -> {sorted(serie.keys())}"
    assert serie["2017-06-30"]["val"] == 110, "el acumulado de 6 meses quedo filtrado (Q2 = 110, no 210)"


# ---------- ingesta_10q.py: regex del MD&A ----------

def test_ingesta_10q_regex_mda():
    from ingesta_10q import (
        PATRON_INICIO_MDA, PATRON_INICIO_MDA_LAXO, PATRON_FIN_MDA,
        PATRON_FIN_MDA_ALT, _es_mencion_cruzada,
    )

    assert PATRON_INICIO_MDA.search("Item 2. Management's Discussion and Analysis") is not None, \
        "apostrofo curvo (el de los filings reales)"
    assert PATRON_INICIO_MDA.search("Item 2. Management's Discussion and Analysis") is not None, \
        "apostrofo recto"
    assert PATRON_INICIO_MDA.search("ITEM 2 MANAGEMENTS DISCUSSION AND ANALYSIS") is not None, \
        "sin posesivo y en mayusculas"
    assert PATRON_INICIO_MDA.search("Item 2: Management's Discussion and Analysis") is not None, \
        "dos puntos en vez de punto tras el numero de item"
    assert PATRON_FIN_MDA.search("Item 3. Quantitative and Qualitative Disclosures") is not None, \
        "patron de fin (Item 3)"
    assert PATRON_FIN_MDA_ALT.search("Item 4. Controls and Procedures") is not None, \
        "patron de fin alternativo (Item 4, cuando no hay Item 3 con ese titulo)"

    # Caso real BYRN: el patron laxo (sin "Item 2" delante) engancho la
    # frase de forward-looking-statements que CITA el titulo del MD&A
    # entre comillas — eso es legitimo, es el arranque real de la seccion.
    assert PATRON_INICIO_MDA_LAXO.search(
        "our Management's Discussion and Analysis of Financial Condition "
        "and results, are forward-looking statements"
    ) is not None, "patron laxo encuentra el titulo sin 'Item 2' delante"

    # Caso real BYRN (el que rompia antes del filtro): una MENCION
    # cruzada en las notas del Item 1, remitiendo al 10-K anual — no es
    # el encabezado real y debe descartarse.
    texto_mencion = (
        "the financial statements should be read in conjunction with "
        "Management's Discussion and Analysis of Financial Condition and "
        "Results of Operations contained in the Company's annual report "
        "on Form 10-K"
    )
    match_mencion = PATRON_INICIO_MDA_LAXO.search(texto_mencion)
    assert match_mencion is not None, "la mencion cruzada SI casa con el patron laxo (por eso hace falta el filtro)"
    assert match_mencion and _es_mencion_cruzada(texto_mencion, match_mencion.start()), \
        "pero el filtro de mencion cruzada la detecta y la descarta"

    texto_encabezado_real = (
        "PART I ITEM 2. Management's Discussion and Analysis of Financial "
        "Condition and Results of Operations. Overview: our revenue grew"
    )
    match_real = PATRON_INICIO_MDA.search(texto_encabezado_real)
    assert match_real and not _es_mencion_cruzada(texto_encabezado_real, match_real.start()), \
        "un encabezado real (via patron principal) no se marca como mencion cruzada"


# ---------- auditor.py: verifica, no da veredicto ----------

def test_auditor_prompt_no_pide_veredicto_ni_ve_el_del_detective():
    """
    Historico real: el Auditor de la v1 declaro EMPEORA en 74 % de los casos (96 de 130;
    su veredicto real fue mas bajo en 45 %), y en 24 de 24 al auditar sus propios
    analisis (mas bajo en 16, igual en 8), y anadirle la tarea de
    buscar fortalezas no lo arreglo: un LLM no da un juicio global estable sobre otro
    analisis. Ahora solo comprueba afirmaciones contra el texto. Este test congela
    que el prompt no vuelva a pedir un veredicto ni ensene el del Detective (que lo
    ancla).
    """
    from auditor import construir_prompt_auditor

    contexto_fake = {
        "nombre": "Empresa Ficticia", "ticker": "FAKE",
        "texto_mda": "El negocio crecio este trimestre.",
        "eventos_8k": [], "activistas": [], "senales_mercado": None,
    }
    respuesta_detective_fake = {
        "catalizador_no_obvio": "x", "tesis_inversion": "y",
        "riesgos": [], "veredicto_preliminar": "MUY_INTERESANTE",
    }
    prompt = construir_prompt_auditor(contexto_fake, respuesta_detective_fake,
                                      [{"cifra": "34%", "contexto": "ingresos +34%"}])

    assert "MUY_INTERESANTE" not in prompt and "veredicto" not in prompt.lower(), \
        "ni pide un veredicto ni muestra el del Detective"
    assert "NO emitas un juicio global" in prompt
    assert "RESPALDADA" in prompt and "SIN_RESPALDO" in prompt and "CONTRADICHA" in prompt
    assert "«34%»" in prompt, "le pasa las cifras que el codigo no ha encontrado"
    assert "riesgos omitidos" in prompt.lower()


# ---------- ingesta_8k.py: items y extraccion de secciones ----------

def test_ingesta_8k_parsear_items():
    from ingesta_8k import parsear_items

    assert parsear_items("1.01, 9.01") == ["1.01", "9.01"]
    assert parsear_items("") == []
    assert parsear_items(None) == []


def test_ingesta_8k_patron_item():
    from ingesta_8k import construir_patron_item

    assert construir_patron_item("1.01").search(
        "Item 1.01 Entry into a Material Definitive Agreement"
    ) is not None
    assert construir_patron_item("1.01").search("Item 1x01 cualquier cosa") is None, \
        "el punto es literal, no comodin (1.01 no casa con 1x01)"
    assert construir_patron_item("5.02").search("ITEM 5.02. DEPARTURE OF DIRECTORS") is not None, \
        "mayusculas del filing real"


def test_ingesta_8k_extraer_item():
    from ingesta_8k import extraer_item_8k

    # Documento sintetico con la estructura tipica de un 8-K real: el
    # item aparece DOS veces (mencion corta de portada/indice y cuerpo
    # real con contenido), luego otro item y el bloque de firmas.
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
    assert aislado_8k and "supply agreement" in texto_8k, \
        "elige el cuerpo real, no la mencion de portada (hueco mas grande)"
    assert "Exhibits" not in texto_8k, "corta en el siguiente item (no arrastra los exhibits)"

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
    assert aislado_ult and "resignation" in texto_ult, \
        "ultimo item del doc: la frontera es el bloque de firmas"
    assert "duly caused" not in texto_ult, "no arrastra el bloque de firmas"

    # Item que el indice declaro pero no aparece en el cuerpo -> fallback
    _, aislado_ausente = extraer_item_8k(doc_8k, "5.02")
    assert not aislado_ausente, "item ausente -> fallback (seccion_aislada=False)"


def test_ingesta_8k_fuente_citas_ampliada():
    from detective import verificar_citas, texto_fuente_citas

    ctx_falso = {
        "texto_mda": "Revenue increased due to strong demand.",
        "eventos_8k": [("1.01", date(2026, 7, 1),
                        "the Company entered into a definitive agreement with ACME Corp")],
        "activistas": [],
        "senales_mercado": None,
    }
    v8k = verificar_citas(
        [{"afirmacion": "contrato", "cita_literal": "definitive agreement with ACME Corp"}],
        texto_fuente_citas(ctx_falso),
    )
    assert v8k["citas_verificadas"] == 1, "cita del 8-K verificada contra la fuente ampliada"


# ---------- ingesta_13dg.py: porcentaje y deteccion de shelf ----------

def test_ingesta_13dg_porcentaje():
    from ingesta_13dg import extraer_pct_participacion

    texto_13g = (
        "CUSIP No. 04271T100\n"
        "11. Aggregate Amount Beneficially Owned by Each Reporting Person: 2,145,678\n"
        "13. Percent of Class Represented by Amount in Row (11): 9.9%\n"
        "14. Type of Reporting Person: IA\n"
    )
    assert extraer_pct_participacion(texto_13g) == 9.9, "extrae el 9.9% de la portada del schedule"
    assert extraer_pct_participacion("no hay nada que extraer aqui") is None
    assert extraer_pct_participacion(None) is None, "sin explotar"

    # El regex exige la frase "percent of class" cerca — un numero
    # suelto con % en otro contexto no debe colar como participacion
    assert extraer_pct_participacion("the interest rate is 12.5% per annum") is None

    # Formato XML nuevo (SCHEDULE 13G desde finales de 2024)
    assert extraer_pct_participacion("<percentOfClass>9.9</percentOfClass>") == 9.9


def test_ingesta_13dg_shelf():
    from ingesta_13dg import es_formulario_shelf

    assert es_formulario_shelf("S-3")
    assert es_formulario_shelf("S-3/A")
    assert es_formulario_shelf("424B5")
    assert not es_formulario_shelf("8-K")
    assert not es_formulario_shelf(None), "sin explotar"


# ---------- notificador_telegram.py ----------

def test_notificador_telegram_sin_configurar(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    from notificador_telegram import enviar_telegram

    assert enviar_telegram("prueba") is False, \
        "sin configurar devuelve False sin lanzar excepcion"
