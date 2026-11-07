"""
tests/test_backup_db.py — la copia de seguridad: cuándo toca, cuántas se guardan, que
una copia a medias o ilegible no se quede como si fuera buena, y que el orquestador
la anote si falla. Docker simulado: no se vuelca nada de verdad.
"""

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import backup_db
import orquestador


def _crear(directorio: Path, nombre: str, hace_horas: float = 0):
    f = directorio / nombre
    f.write_bytes(b"x")
    t = (datetime.now() - timedelta(hours=hace_horas)).timestamp()
    os.utime(f, (t, t))
    return f


def test_el_nombre_lleva_la_fecha_y_hora_y_lo_reconoce_el_patron():
    n = backup_db.nombre_de_copia(datetime(2026, 11, 6, 18, 11, 16))
    assert n == "smallcap_hunter_20261106_181116.dump" and backup_db.PATRON.match(n)


def test_solo_cuentan_las_copias_con_el_nombre_exacto(tmp_path):
    _crear(tmp_path, "smallcap_hunter_20261106_181116.dump")
    _crear(tmp_path, "smallcap_hunter_20261106_181116.dump.parcial")
    _crear(tmp_path, "mis_apuntes.txt")

    assert [f.name for f in backup_db.copias(tmp_path)] == ["smallcap_hunter_20261106_181116.dump"]


def test_sin_copias_o_con_la_ultima_vieja_toca_y_con_una_reciente_no(tmp_path):
    ahora = datetime.now()
    assert backup_db.toca_copia(tmp_path / "no_existe", ahora)
    assert backup_db.toca_copia(tmp_path, ahora)

    _crear(tmp_path, "smallcap_hunter_20261105_090000.dump", hace_horas=30)
    assert backup_db.toca_copia(tmp_path, ahora)

    _crear(tmp_path, "smallcap_hunter_20261106_090000.dump", hace_horas=2)
    assert not backup_db.toca_copia(tmp_path, ahora)


def test_rotar_deja_las_mas_nuevas_y_no_toca_archivos_ajenos(tmp_path):
    for dia in range(1, 11):
        _crear(tmp_path, f"smallcap_hunter_202611{dia:02d}_100000.dump")
    ajeno = _crear(tmp_path, "importante.txt")

    borradas = backup_db.rotar(tmp_path, conservar=7)

    assert len(borradas) == 3 and len(backup_db.copias(tmp_path)) == 7
    assert backup_db.copias(tmp_path)[-1].name == "smallcap_hunter_20261104_100000.dump", "se van las más viejas"
    assert ajeno.exists()


def _docker_falso(volcado=b"datos", lista=b"; Archive...", codigo_dump=0, codigo_lista=0):
    def run(comando, stdout=None, stderr=None, stdin=None, timeout=None, **kw):
        if "pg_dump" in comando:
            if stdout is not None and hasattr(stdout, "write"):
                stdout.write(volcado)
            return subprocess.CompletedProcess(comando, codigo_dump, stdout=b"", stderr=b"fallo del dump")
        return subprocess.CompletedProcess(comando, codigo_lista, stdout=lista, stderr=b"ilegible")
    return run


def test_una_copia_buena_queda_con_su_nombre_y_sin_archivo_parcial(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_db.subprocess, "run", _docker_falso())

    destino = backup_db.hacer_copia(tmp_path, ahora=datetime(2026, 11, 6, 18, 0, 0))

    assert destino.name == "smallcap_hunter_20261106_180000.dump" and destino.exists()
    assert not list(tmp_path.glob("*.parcial"))


@pytest.mark.parametrize("fallo", [
    dict(codigo_dump=1),                 # pg_dump falla
    dict(volcado=b""),                   # no escribe nada
    dict(codigo_lista=1),                # no se puede leer de vuelta
    dict(lista=b""),                     # lee de vuelta pero vacío
])
def test_una_copia_mala_se_borra_y_se_avisa_en_vez_de_dejarla_como_buena(tmp_path, monkeypatch, fallo):
    monkeypatch.setattr(backup_db.subprocess, "run", _docker_falso(**fallo))

    with pytest.raises(RuntimeError):
        backup_db.hacer_copia(tmp_path, ahora=datetime(2026, 11, 6, 18, 0, 0))

    assert list(tmp_path.iterdir()) == [], "ni copia ni parcial"


def test_si_docker_se_cuelga_se_corta_y_no_deja_basura(tmp_path, monkeypatch):
    def cuelga(comando, timeout=None, **kw):
        raise subprocess.TimeoutExpired(comando, timeout)

    monkeypatch.setattr(backup_db.subprocess, "run", cuelga)

    with pytest.raises(RuntimeError, match="pasó de"):
        backup_db.hacer_copia(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_la_fase_no_hace_otra_copia_si_la_ultima_es_reciente(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    _crear(tmp_path, "smallcap_hunter_20261106_090000.dump", hace_horas=1)
    monkeypatch.setattr(backup_db, "hacer_copia", lambda *a, **k: pytest.fail("no debe copiar"))

    assert backup_db.fase_backup() is True


def test_la_fase_devuelve_false_y_no_lanza_si_la_copia_falla(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(backup_db, "hacer_copia", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin docker")))

    assert backup_db.fase_backup() is False


def test_el_orquestador_anota_la_copia_fallida_para_las_alertas(monkeypatch):
    monkeypatch.setattr(backup_db, "fase_backup", lambda: False)
    orquestador.PASOS_FALLIDOS.clear()

    orquestador.fase_backup()

    assert orquestador.PASOS_FALLIDOS == ["Copia de seguridad"]
    orquestador.PASOS_FALLIDOS.clear()


def test_el_contenedor_de_la_base_se_puede_cambiar_para_docker_compose(monkeypatch):
    """Con `docker compose up` el contenedor no se llama smallcap_hunter_db: sin DB_CONTAINER la
    copia automática fallaba en silencio buscándolo."""
    monkeypatch.delenv("DB_CONTAINER", raising=False)
    assert backup_db.contenedor_de_la_base() == "smallcap_hunter_db"
    monkeypatch.setenv("DB_CONTAINER", "smallcaphunter-db-1")
    assert backup_db.contenedor_de_la_base() == "smallcaphunter-db-1"
    monkeypatch.setenv("DB_CONTAINER", "")
    assert backup_db.contenedor_de_la_base() == "smallcap_hunter_db", "vacío cuenta como sin configurar"
