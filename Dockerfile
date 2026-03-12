# Usar una imagen oficial de Python ligera
FROM python:3.10-slim

# Evitar que Python genere archivos .pyc y forzar el log a la consola
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Directorio de trabajo en el contenedor
WORKDIR /app

# Instalar dependencias del sistema necesarias para PostgreSQL (u otros si usas librerías compiladas)
RUN apt-get update \
    && apt-get install -y gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar primero el requirements para aprovechar la caché de capas de Docker
COPY requirements.txt .

# Instalar dependencias 
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copiar el resto del código del proyecto
COPY . .

# Exponer el puerto que usará Flask/Gunicorn
EXPOSE 5000

# Comando para correr en producción usando Gunicorn (3 workers recomendado)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "3", "run:app"]
