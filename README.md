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

Para incluir el perfil PostgreSQL descartable y las migraciones US-010A:

```powershell
..\scripts\test.ps1 -Component backend -Postgres
```

## Migraciones

Flask-Migrate/Alembic es el mecanismo productivo de esquema. Una base local
nueva y descartable de pruebas se crea con:

```powershell
flask --app app db upgrade head
```

El PostgreSQL local se usa solamente para pruebas automatizadas y validaciones
de migraciones. No representa una base de aplicación ni autoriza operaciones
sobre la base desplegada. `crear_tablas.py` tampoco es un flujo productivo: no
debe sustituir `flask db upgrade` ni ejecutarse sobre una base con datos.

Una base legacy sin `alembic_version` no debe ejecutar ese comando directamente.
Primero se restaura una copia aislada, se verifica contra la revisión baseline y
solo entonces se simula `stamp` seguido de `upgrade`. El procedimiento y sus
guardas están en `migrations/README`.

La revisión contract `58b3dd5878cd` exige un despliegue en dos pasos. Primero se
despliega el código que hace dual-write transaccional de `MateriaPrima` o
`Colorante` junto con su `ScmMaterial`. Después se drenan y detienen todas las
instancias antiguas que todavía puedan escribir filas legacy sin identidad
común y se pausan temporalmente todas las altas de catálogo, incluidas las de
instancias nuevas; solo entonces se ejecuta `flask --app app db upgrade head`.

El rollback de `58b3dd5878cd` vuelve `scm_material_id` a nullable, pero conserva
los vínculos y materiales comunes ya creados. No elimina ni reconstruye esas
identidades. `SCM_RECEPCION_ENABLED` debe permanecer en `false` durante el
despliegue, la migración y cualquier rollback de este incremento.

El primer incremento US-010A siembra roles y capacidades configurables, pero no
los asigna a trabajadores. Además, `SCM_RECEPCION_ENABLED` es `false` por defecto.

## API SCM disponible en desarrollo local

El corte actual expone `/api/scm/v1` con captura de borradores, pero sin
habilitar todavía confirmación de custodia ni inventario. `X-Actor-Id` debe
identificar un `Trabajador` activo y el servidor
deriva sus capacidades desde roles persistidos; el body nunca decide permisos.

- `GET/POST/PATCH /config/categorias-recepcion[/{id}]`: CRUD lógico y
  versionado de modalidades de recepción.
- `GET/POST/PATCH /materiales[/{id}]`: identidad común y dual-write atómico con
  `MateriaPrima` o `Colorante`.
- `GET/POST/PATCH /proveedores[/{id}]`: catálogo lógico de proveedores.
- `GET/POST/PATCH /documentos-proveedor[/{id}]`: identidad externa única y
  versionada; una misma guía o factura puede vincularse a varias recepciones.
- `GET/POST/PATCH /recepciones/materiales[/{id}]`: borradores versionados con
  documentos N:M y pesaje persistente por cada bolsa de material de segunda.
- `GET/POST /ordenes-compra-material` y `GET
  /ordenes-compra-material/{id}`: cabecera y revisión inicial.
- `POST /ordenes-compra-material/{id}/revisiones` y `PATCH
  /ordenes-compra-material/{id}/revisiones/{numero}`: nueva revisión y edición
  completa de líneas mientras permanezca `BORRADOR`.
- `POST /ordenes-compra-material/{id}/enviar-aprobacion` y `POST
  /ordenes-compra-material/{id}/aprobar`: transiciones idempotentes que exigen
  `Idempotency-Key` UUID y segregan Compras de Gerencia.

Las mutaciones de catálogo y borrador generan `scm_evento`; las transiciones
idempotentes además generan `scm_operacion`. Una OC puede conservar en borrador
un material pendiente de configurar, pero no puede enviarlo ni aprobarlo hasta
que material y categoría estén activos y recibibles. El saldo recibido real
permanece en `0.000` hasta implementar las imputaciones de recepción.
Los borradores no crean lote, sticker, movimiento ni saldo. Esos efectos sólo
se incorporarán en la transición idempotente de confirmación.

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

