# envaperu-workflow
# file structure
```
envaperu_workflow/
├── .env                    # Variables de entorno (URL de Supabase aquí)
├── .gitignore
├── requirements.txt        # Dependencias (flask, sqlalchemy, psycopg2, etc.)
├── run.py                  # Punto de entrada (Entry point)
├── migrations/             # Carpeta generada por Flask-Migrate (NO la creas tú)
└── app/
    ├── __init__.py         # Aquí vive la función create_app()
    ├── config.py           # Configuraciones (Dev, Prod)
    ├── extensions.py       # Instancias de db, migrate, cors (Evita import circular)
    │
    ├── models/             # TUS TABLAS (El corazón del ORM)
    │   ├── __init__.py     # Para exportar modelos limpiamente
    │   ├── orden.py        # Entidad OrdenProduccion (y la herencia)
    │   ├── lote.py         # Entidad LoteColor
    │   └── materiales.py   # Insumos y Recetas
    │
    ├── services/           # LÓGICA DE NEGOCIO (Aquí van las fórmulas)
    │   ├── __init__.py
    │   └── produccion_service.py  # Aquí calculas Kilos -> Coladas
    │
    ├── api/                # ENDPOINTS (Blueprints)
    │   ├── __init__.py
    │   └── rutas_produccion.py    # POST /ordenes, GET /ordenes
    │
    └── utils/              # Herramientas genéricas
        └── validadores.py
```

## Pruebas

Instalar dependencias productivas y de desarrollo en un entorno Python 3.12:

```powershell
pip install -r requirements-dev.txt
```

La ejecución predeterminada es la suite rápida y excluye E2E y PostgreSQL:

```powershell
python -m pytest
```

Los marcadores disponibles son `contract`, `e2e` y `postgres`. La prueba del contrato proveedor puede ejecutarse de forma aislada con:

```powershell
python -m pytest tests/test_sync_contract.py
```

Desde el workspace maestro se recomienda usar `..\scripts\test.ps1 -Component backend`.

## Monitoreo de Estaciones de Pesaje

El backend central acepta capabilities y heartbeats de estaciones previamente
provisionadas. Antes del primer despliegue, crear solo las tablas de este modulo:

```powershell
python scripts\migrate_station_monitoring.py
```

La migracion es idempotente y no ejecuta `db.create_all()` sobre el resto del
SCM. Luego registrar el UUID persistido por la estacion:

```powershell
flask --app run.py provision-weighing-station `
  --station-id <UUID> `
  --code PESAJE-PLANTA-01 `
  --name "Balanza principal" `
  --location "Planta - pesaje"
```

El comando imprime `TOKEN_ONCE` una sola vez. Central conserva solamente su hash
SHA-256; el valor original debe introducirse en la estacion mediante
`station_control.py provision-token` para que Windows lo cifre con DPAPI.

Rutas implementadas:

- `GET /api/integration/v1/capabilities`;
- `PUT /api/integration/v1/stations/{station_id}/heartbeat`;
- `GET /api/monitoring/v1/weighing-stations`;
- `GET /api/monitoring/v1/weighing-stations/{station_id}`.

El heartbeat es idempotente y solo actualiza el modelo de observabilidad. Nunca
crea `ControlPeso`, no sincroniza inventario y no ofrece control remoto de la
balanza o impresora. Las rutas de lectura del monitor aun requieren integrarse
con la autenticacion humana del frontend central antes de exponerse fuera de una
red administrativa controlada.

