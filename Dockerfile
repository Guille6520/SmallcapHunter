# SmallCap Hunter — imagen para la API y el dashboard.
# Python 3.12 porque requirements.txt fija versiones (pandas 2.2.3...) con ruedas
# publicadas para 3.12; las claves (.env) NO van dentro de la imagen: las pone
# docker-compose.yml al arrancar.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Las dependencias primero: mientras requirements.txt no cambie, esta capa se reutiliza
# y reconstruir tras tocar el código tarda segundos.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Por defecto, la API. docker-compose.yml cambia el comando para el dashboard.
EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
