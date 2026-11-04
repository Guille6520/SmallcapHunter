"""
tests/test_contexto_sector.py — el mapa SIC -> sector, el movimiento del ETF frente
al IWM, el parseo de titulares y, sobre todo, que los análisis NO lean el contexto
de sector (los Detectives y el Auditor son ciegos a propósito). Sin base de datos ni red.
"""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

import contexto_sector
from contexto_sector import (
    movimiento_frente_al_benchmark, parsear_google_rss, parsear_yahoo, retornos,
)
from sectores import BENCHMARK, RANGOS_SIC, SECTORES, etf_de_sector, etfs_a_seguir, sector_de_sic

RAIZ = Path(__file__).resolve().parent.parent


# ---------- mapa de sectores ----------

@pytest.mark.parametrize("sic, sector", [
    ("2834", "biotecnologia_farma"), ("2836", "biotecnologia_farma"), ("3841", "dispositivos_medicos"),
    ("7372", "software_ti"), ("7370", "software_ti"), ("3674", "semiconductores"),
    ("6022", "bancos"), ("6798", "inmobiliario_reit"), ("6770", "spac"), ("1040", "mineria_oro"),
    ("1311", "petroleo_gas"), ("1000", "metales_mineria"), ("4911", "utilities"), ("4813", "telecom_medios"),
    ("7363", "industria"), ("5812", "consumo_discrecional"), ("5411", "comercio"), ("2086", "alimentos_agricultura"),
    ("0100", "alimentos_agricultura"), ("2911", "energia"), ("6331", "seguros"), ("6199", "finanzas"),
])
def test_sic_conocidos_caen_en_su_sector(sic, sector):
    assert sector_de_sic(sic) == sector


def test_los_casos_especiales_ganan_al_rango_general():
    assert sector_de_sic("6770") == "spac"            # no "finanzas"
    assert sector_de_sic("6798") == "inmobiliario_reit"   # no "finanzas"
    assert sector_de_sic("1040") == "mineria_oro"     # no "metales_mineria"
    assert sector_de_sic("3674") == "semiconductores"  # no "hardware_electronica"
    assert sector_de_sic("5812") == "consumo_discrecional"  # restaurantes, no "comercio"


@pytest.mark.parametrize("sic", [None, "", "abc", "   ", "99999", "0", "9999"])
def test_sic_ausente_o_raro_cae_en_otros(sic):
    assert sector_de_sic(sic) == "otros"


def test_sic_con_ceros_o_como_numero():
    assert sector_de_sic("0100") == sector_de_sic(100) == sector_de_sic(" 100 ")


def test_cada_rango_apunta_a_un_sector_que_existe_y_esta_bien_formado():
    for desde, hasta, clave in RANGOS_SIC:
        assert desde <= hasta, (desde, hasta)
        assert clave in SECTORES, clave


def test_los_sectores_con_busqueda_tienen_etf_y_los_demas_no():
    for clave, d in SECTORES.items():
        if d["busqueda"]:
            assert d["etf"], clave
    assert etf_de_sector("spac") is None and etf_de_sector("otros") is None
    assert etf_de_sector("inventado") is None   # clave desconocida: como "otros"


def test_etfs_a_seguir_incluye_el_benchmark_sin_repetidos():
    etfs = etfs_a_seguir()
    assert BENCHMARK in etfs and len(etfs) == len(set(etfs))
    assert all(isinstance(e, str) and e.isupper() for e in etfs)


# ---------- movimiento del sector ----------

def _serie(n, inicio=100.0, paso=1.0, desde=date(2025, 1, 1)):
    return [(desde + timedelta(days=i), inicio + paso * i) for i in range(n)]


def test_retornos_cuenta_dias_de_mercado_hacia_atras():
    r = retornos(_serie(300))     # sube 1 punto por día desde 100
    assert r["1 mes"] == pytest.approx((100 + 299) / (100 + 299 - 21) - 1)
    assert r["3 meses"] == pytest.approx((100 + 299) / (100 + 299 - 63) - 1)
    assert r["12 meses"] == pytest.approx((100 + 299) / (100 + 299 - 252) - 1)


def test_retornos_no_inventa_si_falta_historia():
    r = retornos(_serie(30))
    assert r["1 mes"] is not None and r["3 meses"] is None and r["12 meses"] is None
    assert all(v is None for v in retornos([]).values())


def test_movimiento_resta_el_benchmark_y_usa_fechas_comunes():
    etf = _serie(100, paso=2.0)
    bench = _serie(100, paso=1.0)
    m = movimiento_frente_al_benchmark(etf, bench)["3 meses"]
    assert m["etf"] == pytest.approx(etf[-1][1] / etf[-1 - 63][1] - 1)
    assert m["dif"] == pytest.approx(m["etf"] - m["iwm"])
    assert m["dif"] > 0


def test_movimiento_con_un_dia_menos_en_el_benchmark_no_descuadra_fechas():
    etf = _serie(100)
    bench = [x for i, x in enumerate(_serie(100)) if i != 50]   # falta un día
    m = movimiento_frente_al_benchmark(etf, bench)["1 mes"]
    assert m["etf"] is not None and m["iwm"] is not None


def test_movimiento_sin_datos():
    m = movimiento_frente_al_benchmark([], _serie(10))
    assert all(v == {"etf": None, "iwm": None, "dif": None} for v in m.values())


# ---------- titulares ----------

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss><channel>
<item><title>Biotech rallies on rate hopes - Reuters</title><link>https://g.test/1</link>
  <pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title>Repetido - Reuters</title><link>https://g.test/1</link><source>Reuters</source></item>
<item><title></title><link>https://g.test/2</link></item>
<item><title>Sin enlace</title></item>
<item><title>Fecha rara sin fuente</title><link>https://g.test/3</link><pubDate>ayer</pubDate></item>
</channel></rss>"""


def test_parsear_google_rss():
    n = parsear_google_rss(RSS)
    assert [x["titular"] for x in n] == ["Biotech rallies on rate hopes", "Fecha rara sin fuente"]
    assert n[0]["fuente"] == "Reuters" and n[0]["fecha"].year == 2026 and n[0]["origen"] == "google_news"
    assert n[1]["fuente"] is None and n[1]["fecha"] is None


def test_parsear_google_rss_con_algo_que_no_es_un_rss():
    assert parsear_google_rss("Please limit requests") == []
    assert parsear_google_rss("") == [] and parsear_google_rss(None) == []


def test_parsear_google_rss_respeta_el_maximo():
    items = "".join(f"<item><title>t{i}</title><link>https://g.test/{i}</link></item>" for i in range(20))
    assert len(parsear_google_rss(f"<rss><channel>{items}</channel></rss>", maximo=5)) == 5


def test_parsear_yahoo_formato_nuevo_y_viejo():
    items = [
        {"content": {"title": "Nuevo", "canonicalUrl": {"url": "https://y.com/n"},
                     "pubDate": "2026-11-03T10:00:00Z", "provider": {"displayName": "Reuters"}}},
        {"title": "Viejo", "link": "https://y.com/v", "providerPublishTime": 1793000000, "publisher": "AP"},
        {"content": {"title": "Sin enlace"}},
    ]
    n = parsear_yahoo(items)
    assert [x["titular"] for x in n] == ["Nuevo", "Viejo"]
    assert n[0]["fuente"] == "Reuters" and n[0]["fecha"].year == 2026 and n[0]["origen"] == "yahoo"
    assert n[1]["fuente"] == "AP" and n[1]["fecha"] is not None


def test_la_fuente_que_falla_devuelve_lista_vacia(monkeypatch):
    def explota(*a, **k):
        raise RuntimeError("sin red")

    monkeypatch.setattr(contexto_sector.requests, "get", explota)
    assert contexto_sector.titulares_google("(biotech)") == []

    class _Limite:
        status_code = 429
        text = "Please limit requests"

        def raise_for_status(self):
            raise RuntimeError("429")

    monkeypatch.setattr(contexto_sector.requests, "get", lambda *a, **k: _Limite())
    assert contexto_sector.titulares_google("(biotech)") == []


def test_la_fase_nunca_rompe_la_pasada(monkeypatch):
    import orquestador

    def sin_bd():
        raise RuntimeError("sin conexión")

    monkeypatch.setattr(orquestador, "conectar_db", sin_bd)
    assert contexto_sector.fase_sectores() == {}


# ---------- ciegos por diseño ----------

# Los módulos que producen o deciden un análisis. Ninguno puede leer el contexto de
# sector: si lo hiciera, los Detectives podrían reconocer la empresa o sesgarse con
# noticias, y el veredicto dejaría de ser comparable con los anteriores.
MODULOS_CIEGOS = [
    "detective.py", "auditor.py", "grafo_capa3.py", "scorer_capa2.py", "filtro_capa1.py",
    "verificador_citas.py", "embeddings.py",
]


@pytest.mark.parametrize("modulo", MODULOS_CIEGOS)
def test_los_analisis_no_leen_el_contexto_de_sector(modulo):
    ruta = RAIZ / modulo
    assert ruta.exists(), f"{modulo} no existe: actualiza la lista de módulos ciegos"
    codigo = ruta.read_text(encoding="utf-8")
    assert not re.search(r"contexto_sector|sector_noticias|sector_precios|import sectores|from sectores", codigo), (
        f"{modulo} no debe leer el contexto de sector: los análisis son ciegos a propósito"
    )
