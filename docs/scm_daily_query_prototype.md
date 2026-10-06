# Prototipo local de consulta diaria SCM

Este incremento habilita unicamente la intencion `production_daily_summary`.
La fecha se resuelve en `America/Lima`; `ayer` es la fecha local anterior a
`now`, y la lectura usa la ventana de Lima convertida a UTC.

El adaptador local ejecuta una consulta SQLAlchemy de forma fija sobre
`ScmPesajeManga`, con limite de filas y una transaccion PostgreSQL
`REPEATABLE READ`/`READ ONLY` (o reloj local en SQLite). Agrupa pesajes
efectivos por OF/color y expone las anulaciones en campos separados. La
correccion aplicada mas reciente se usa como peso efectivo.

La autorizacion se valida en el servidor: el actor debe estar activo y tener
`OT_VER`, `MANGA_PESAJE_VER` y `ASISTENTE_PRODUCCION_USAR`. El acceso se
reasigna cambiando esa capacidad persistida; el servicio no concede
capacidades ni usa una lista de actores. La suma diaria no se compara con la
meta de una OF: el avance/meta compatible pertenece a
`list_production_progress`.

El cache vive solo en memoria y usa actor, alcance de permisos, filtros y
version como clave. Tiene TTL e invalidacion por actor. El log local conserva
unicamente trace ID, actor, intencion, fecha, latencia, cache hit y estado; no
guarda payloads, tokens ni pretende medir ahorro de tokens.

La entrada remota queda representada por `PendingAuthAdapter`, que devuelve
`SCM_AUTH_PENDING` hasta que exista una autenticacion aprobada. No hay OAuth,
credenciales, persistencia de secretos ni mutaciones productivas.

## CLI local

Con dependencias del backend instaladas y una base local autorizada:

```powershell
python -m app.services.scm_daily_query_service --intent production_daily_summary --actor-id <id> --date ayer
```

El comando solo acepta la intencion indicada y un actor explicito. La
autorizacion por capacidades ocurre dentro del servicio. No expone SQL ni
shell al modelo.

## Verificacion

La prueba focal es `tests/test_scm_daily_query_service.py`. La verificacion
local se ejecuta con el Python del backend:

```powershell
C:\Users\esteb\gitprojects\envaperu-workspace-2\backend\.venv\Scripts\python.exe -m pytest tests/test_scm_daily_query_service.py tests/test_scm_assistant_http.py -q
```

La integracion UI y la autenticacion remota quedan pendientes.
