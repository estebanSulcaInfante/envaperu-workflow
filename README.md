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

