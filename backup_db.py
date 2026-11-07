"""
backup_db.py — copia de seguridad de la base de datos.

Lo único realmente irreemplazable del proyecto es lo que está en Postgres: los
análisis pagados de los Detectives, el historial de la tesis y los precios de
seguimiento. El código está en git; la base, no. Un `docker compose down -v`, un
disco que falla o un `drop` sin querer, y se pierde todo.

Hace un volcado con pg_dump (formato comprimido, restaurable con pg_restore) del
contenedor de Docker, lo comprueba leyéndolo de vuelta (pg_restore --list) y borra
los más viejos, dejando los últimos N. Si la comprobación falla, el archivo se
borra y se avisa: una copia que no se puede leer es peor que ninguna, porque da
falsa tranquilidad.

El orquestador lo lanza al empezar cada pasada si la última copia tiene más de 20 h.
Para que sobreviva a un fallo del disco, apunta BACKUP_DIR a otro sitio (OneDrive,
un disco externo...) en el .env.

Cómo usarlo:
  python backup_db.py              # hace la copia ahora
  python backup_db.py --listar     # qué copias hay

Para restaurar en una base vacía (por ejemplo, tras perderlo todo):
  docker exec -i smallcap_hunter_db pg_restore -U postgres -d smallcap_hunter --clean --if-exists < backups/ARCHIVO.dump
"""

import os
import re
import sys
import time
import logging
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

AQUI = Path(__file__).resolve().parent
CONTENEDOR = "smallcap_hunter_db"   # nombre por defecto; con Docker Compose se cambia con DB_CONTAINER
CONSERVAR = 7
HORAS_ENTRE_COPIAS = 20
TIMEOUT_SEGUNDOS = 30 * 60
PATRON = re.compile(r"^smallcap_hunter_\d{8}_\d{6}\.dump$")


# ---------- lógica pura ----------

def nombre_de_copia(ahora: datetime) -> str:
    return f"smallcap_hunter_{ahora:%Y%m%d_%H%M%S}.dump"


def copias(directorio: Path) -> list:
    """Las copias que hay, de la más nueva a la más vieja. Solo cuentan los archivos con
    el nombre exacto que pone este script: nunca toco ni borro nada que no sea mío."""
    if not directorio.is_dir():
        return []
    return sorted((f for f in directorio.iterdir() if PATRON.match(f.name)), reverse=True)


def toca_copia(directorio: Path, ahora: datetime, horas: int = HORAS_ENTRE_COPIAS) -> bool:
    """¿Hace falta una copia nueva? Sí si no hay ninguna o la última tiene más de `horas`."""
    existentes = copias(directorio)
    if not existentes:
        return True
    ultima = datetime.fromtimestamp(existentes[0].stat().st_mtime)
    return (ahora - ultima).total_seconds() > horas * 3600


def rotar(directorio: Path, conservar: int = CONSERVAR) -> list:
    """Borra las copias más viejas, dejando las `conservar` más nuevas. Devuelve las borradas."""
    sobrantes = copias(directorio)[conservar:]
    for f in sobrantes:
        f.unlink()
    return sobrantes


# ---------- las copias ----------

def directorio_de_copias() -> Path:
    return Path(os.getenv("BACKUP_DIR") or AQUI / "backups")


def contenedor_de_la_base() -> str:
    """El contenedor de Docker donde corre Postgres. Por defecto `smallcap_hunter_db` (el que se crea
    a mano, ver el README). Con `docker compose up` el contenedor se llama distinto
    (p. ej. `smallcaphunter-db-1`: míralo con `docker compose ps`), y entonces se pone su nombre en
    DB_CONTAINER del .env. Sin esto, la copia automática buscaría un contenedor que no existe."""
    return os.getenv("DB_CONTAINER") or CONTENEDOR


def hacer_copia(directorio: Path = None, contenedor: str = None, ahora: datetime = None) -> Path:
    """Vuelca la base, la comprueba y rota las viejas. Devuelve la ruta de la copia o
    lanza RuntimeError si algo falla (sin dejar un archivo a medias)."""
    from dotenv import load_dotenv
    load_dotenv()
    directorio = directorio or directorio_de_copias()
    directorio.mkdir(parents=True, exist_ok=True)
    contenedor = contenedor or contenedor_de_la_base()
    ahora = ahora or datetime.now()
    base = os.getenv("DB_NAME", "smallcap_hunter")
    usuario = os.getenv("DB_USER", "postgres")

    destino = directorio / nombre_de_copia(ahora)
    parcial = destino.with_suffix(".dump.parcial")   # no cuenta como copia hasta estar entera
    inicio = time.time()
    try:
        with open(parcial, "wb") as f:
            r = subprocess.run(["docker", "exec", contenedor, "pg_dump", "-U", usuario, "-Fc", base],
                               stdout=f, stderr=subprocess.PIPE, timeout=TIMEOUT_SEGUNDOS)
        if r.returncode != 0:
            raise RuntimeError(f"pg_dump falló: {r.stderr.decode(errors='replace').strip()[:300]}")
        if parcial.stat().st_size == 0:
            raise RuntimeError("pg_dump no escribió nada")

        with open(parcial, "rb") as f:
            v = subprocess.run(["docker", "exec", "-i", contenedor, "pg_restore", "--list"],
                               stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_SEGUNDOS)
        if v.returncode != 0 or not v.stdout.strip():
            raise RuntimeError(f"la copia no se puede leer de vuelta: {v.stderr.decode(errors='replace').strip()[:300]}")
        parcial.replace(destino)
    except subprocess.TimeoutExpired:
        parcial.unlink(missing_ok=True)
        raise RuntimeError(f"la copia pasó de {TIMEOUT_SEGUNDOS // 60} min y la corté")
    except Exception:
        parcial.unlink(missing_ok=True)
        raise

    borradas = rotar(directorio)
    log.info(f"Copia de seguridad: {destino.name} ({destino.stat().st_size / 1e6:.0f} MB, "
             f"{time.time() - inicio:.0f} s); borro {len(borradas)} vieja(s)")
    return destino


def fase_backup() -> bool:
    """La pasada del orquestador: copia si toca. Devuelve False si falló. Nunca lanza."""
    try:
        directorio = directorio_de_copias()
        if not toca_copia(directorio, datetime.now()):
            log.info("Copia de seguridad: la última es reciente, no hace falta otra")
            return True
        hacer_copia(directorio)
        return True
    except Exception as e:
        log.warning(f"Copia de seguridad: NO se pudo hacer ({e})")
        return False


def main():
    parser = argparse.ArgumentParser(description="Copia de seguridad de la base de datos")
    parser.add_argument("--listar", action="store_true", help="enseña las copias que hay y no hace ninguna")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

    directorio = directorio_de_copias()
    if args.listar:
        existentes = copias(directorio)
        for f in existentes:
            print(f"{f.name}  {f.stat().st_size / 1e6:.0f} MB")
        if not existentes:
            print(f"No hay copias en {directorio}")
        return
    try:
        print(hacer_copia(directorio))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
