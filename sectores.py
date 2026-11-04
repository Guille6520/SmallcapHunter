"""
sectores.py — el mapa que uso para pasar del código SIC de la SEC al sector de una
empresa y, con él, a un ETF de referencia y a unas palabras para buscar noticias.

Lo hago así porque las small caps casi no tienen cobertura de prensa, pero su sector
sí: hay ETFs y grandes empresas que se mueven por las mismas razones. Me sirve de
guía para el contexto de cada candidata, nada más.

IMPORTANTE (diseño): esto es información para quien mira el dashboard. Ni los
Detectives, ni el Auditor, ni el score leen este módulo ni las tablas que alimenta:
los análisis son ciegos a propósito, y si les diera noticias del sector podrían
reconocer la empresa o sesgar el veredicto. Hay un test que lo vigila
(tests/test_contexto_sector.py).

Cómo está hecho: una lista de rangos de SIC en orden. Cojo el primer rango que
contiene el código, así que los casos especiales (una SPAC, el oro, los REIT) van
ANTES que el rango general donde caerían. Lo que no encaja en ningún rango queda en
"otros", sin ETF propio (se compara solo con el IWM).
"""

from typing import Optional

BENCHMARK = "IWM"  # el Russell 2000, la vara de medir de las small caps

# clave -> datos del sector. "etf" es el fondo cotizado con el que comparo el sector;
# "busqueda" es la consulta para buscar titulares del tema (inglés, que es donde
# está la prensa de estas empresas).
SECTORES = {
    "biotecnologia_farma":   {"nombre": "Biotecnología y farma", "etf": "XBI",
                              "busqueda": '(biotech OR "FDA approval" OR "clinical trial")'},
    "dispositivos_medicos":  {"nombre": "Dispositivos médicos", "etf": "IHI",
                              "busqueda": '("medical device" OR "FDA clearance")'},
    "salud_servicios":       {"nombre": "Servicios de salud", "etf": "IHF",
                              "busqueda": '("healthcare services" OR hospitals OR Medicare)'},
    "software_ti":           {"nombre": "Software y servicios informáticos", "etf": "IGV",
                              "busqueda": '(software OR "cloud computing" OR SaaS)'},
    "semiconductores":       {"nombre": "Semiconductores", "etf": "SMH",
                              "busqueda": '(semiconductor OR "chip makers")'},
    "hardware_electronica":  {"nombre": "Hardware y electrónica", "etf": "XLK",
                              "busqueda": '("electronics makers" OR "computer hardware" OR "data center equipment")'},
    "bancos":                {"nombre": "Bancos", "etf": "KRE",
                              "busqueda": '("regional banks" OR "bank lending")'},
    "finanzas":              {"nombre": "Finanzas", "etf": "XLF",
                              "busqueda": '("financial services" OR "asset managers" OR "consumer lending")'},
    "seguros":               {"nombre": "Seguros", "etf": "KIE",
                              "busqueda": '(insurance OR insurers OR reinsurance)'},
    "inmobiliario_reit":     {"nombre": "Inmobiliario y REIT", "etf": "XLRE",
                              "busqueda": '(REIT OR "commercial real estate" OR "housing market" OR "mortgage rates")'},
    "petroleo_gas":          {"nombre": "Petróleo y gas (extracción)", "etf": "XOP",
                              "busqueda": '("oil prices" OR "natural gas" OR "oil and gas producers")'},
    "energia":               {"nombre": "Energía (refino y servicios)", "etf": "XLE",
                              "busqueda": '(refining OR "energy sector" OR "oilfield services")'},
    "mineria_oro":           {"nombre": "Minería de oro y plata", "etf": "GDX",
                              "busqueda": '("gold price" OR "gold miners" OR "silver price")'},
    "metales_mineria":       {"nombre": "Metales y minería", "etf": "XME",
                              "busqueda": '("copper prices" OR "metals prices" OR "mining stocks")'},
    "materiales_quimica":    {"nombre": "Materiales y química", "etf": "XLB",
                              "busqueda": '(chemicals OR "materials sector" OR packaging)'},
    "industria":             {"nombre": "Industria y servicios empresariales", "etf": "XLI",
                              "busqueda": '("industrial sector" OR manufacturing OR aerospace OR staffing)'},
    "transporte":            {"nombre": "Transporte", "etf": "IYT",
                              "busqueda": '(trucking OR railroad OR airlines OR "shipping rates")'},
    "utilities":             {"nombre": "Servicios públicos", "etf": "XLU",
                              "busqueda": '("electric utilities" OR "power grid" OR "utility rates")'},
    "telecom_medios":        {"nombre": "Telecomunicaciones y medios", "etf": "XLC",
                              "busqueda": '(telecom OR "media industry" OR streaming OR broadcasting)'},
    "comercio":              {"nombre": "Comercio minorista y mayorista", "etf": "XRT",
                              "busqueda": '(retail OR "consumer spending" OR retailers)'},
    "alimentos_agricultura": {"nombre": "Alimentación y agricultura", "etf": "XLP",
                              "busqueda": '("food prices" OR agriculture OR beverage OR packaged food)'},
    "consumo_discrecional":  {"nombre": "Consumo discrecional y ocio", "etf": "XLY",
                              "busqueda": '(restaurants OR travel OR hotels OR "consumer discretionary")'},
    "spac":                  {"nombre": "SPAC / sociedades vehículo", "etf": None, "busqueda": None},
    "otros":                 {"nombre": "Otros", "etf": None, "busqueda": None},
}

# (desde, hasta, clave), ambos extremos incluidos, en orden de prioridad.
RANGOS_SIC = [
    # --- casos especiales primero ---
    (6770, 6770, "spac"),
    (6798, 6798, "inmobiliario_reit"),
    (1040, 1040, "mineria_oro"),
    (3674, 3674, "semiconductores"),
    (5800, 5899, "consumo_discrecional"),          # restaurantes, antes del comercio general
    # --- salud ---
    (2833, 2836, "biotecnologia_farma"),
    (8731, 8731, "biotecnologia_farma"),           # investigación biológica por contrato
    (3841, 3851, "dispositivos_medicos"),
    (3826, 3826, "dispositivos_medicos"),          # instrumentos de análisis de laboratorio
    (8000, 8099, "salud_servicios"),
    # --- tecnología ---
    (7370, 7379, "software_ti"),
    (3570, 3579, "hardware_electronica"),
    (3600, 3699, "hardware_electronica"),
    # --- finanzas ---
    (6020, 6039, "bancos"),
    (6100, 6299, "finanzas"),
    (6300, 6499, "seguros"),
    (6500, 6599, "inmobiliario_reit"),
    # --- energía y materiales ---
    (1300, 1399, "petroleo_gas"),
    (2900, 2999, "energia"),
    (4610, 4619, "energia"),                       # oleoductos
    (1000, 1499, "metales_mineria"),
    (3300, 3399, "metales_mineria"),
    (2600, 2699, "materiales_quimica"),
    (2800, 2899, "materiales_quimica"),
    (3000, 3099, "materiales_quimica"),
    (3200, 3299, "materiales_quimica"),
    # --- industria y transporte ---
    (1500, 1799, "industria"),
    (3400, 3569, "industria"),
    (3580, 3599, "industria"),
    (3700, 3799, "industria"),
    (3800, 3999, "industria"),                     # instrumentos no médicos, manufacturas varias
    (7300, 7369, "industria"),                     # servicios a empresas, empleo temporal
    (7380, 7399, "industria"),
    (8700, 8799, "industria"),
    (4000, 4799, "transporte"),
    # --- servicios públicos, comunicaciones y medios ---
    (4900, 4999, "utilities"),
    (4800, 4899, "telecom_medios"),
    (2700, 2799, "telecom_medios"),
    (7800, 7899, "telecom_medios"),
    # --- consumo ---
    (5000, 5999, "comercio"),
    (100, 999, "alimentos_agricultura"),
    (2000, 2199, "alimentos_agricultura"),
    (2200, 2399, "consumo_discrecional"),
    (2500, 2599, "consumo_discrecional"),
    (3100, 3199, "consumo_discrecional"),
    (7000, 7099, "consumo_discrecional"),
    (7900, 7999, "consumo_discrecional"),
    (8200, 8299, "consumo_discrecional"),
]


def sector_de_sic(sic) -> str:
    """Devuelvo la clave de sector de un código SIC (texto o número, con o sin ceros
    delante). Si no hay código o no encaja en ningún rango, "otros"."""
    try:
        codigo = int(str(sic).strip())
    except (TypeError, ValueError):
        return "otros"
    for desde, hasta, clave in RANGOS_SIC:
        if desde <= codigo <= hasta:
            return clave
    return "otros"


def etf_de_sector(clave: str) -> Optional[str]:
    return SECTORES.get(clave, SECTORES["otros"])["etf"]


def etfs_a_seguir() -> list:
    """Los ETFs distintos de todos los sectores, más el benchmark, para bajar sus
    precios una sola vez cada uno."""
    return sorted({d["etf"] for d in SECTORES.values() if d["etf"]} | {BENCHMARK})
