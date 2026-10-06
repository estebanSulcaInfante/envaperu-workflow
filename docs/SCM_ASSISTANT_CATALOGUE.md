# Catálogo SCM del asistente v2

Este módulo ofrece un catálogo determinista y de solo lectura para el chat de
SCM. El texto en español se convierte en un plan cerrado de hasta dos
consultas, separado con una conjunción explícita como `y además`. El plan se
valida completo antes de abrir una lectura.

## Intenciones permitidas

| Intención | Datos requeridos | Fuente canónica |
| --- | --- | --- |
| `production_daily_summary` | una fecha Lima (`hoy`, `ayer` o ISO) | `scm_daily_query_service` |
| `production_order_progress` | código exacto de OF y color opcional exacto | `list_production_progress` |
| `weighing_period` | inicio y fin explícitos, máximo siete días y 500 lecturas | lectura diaria SCM acotada |
| `manga_trace` | UUID público, código exacto o id de manga | `get_manga_detail` |

`plan_query(query, now=None)` devuelve `answered`, `needs_clarification` o
`unsupported`. Las solicitudes de escritura, SQL, shell, inyección o campos
desconocidos no producen un plan ejecutable. Una fecha o entidad ausente,
inválida o ambigua se devuelve como aclaración estructurada.

`execute_plan(session, actor_id, plan, cache=None, refresh=False)` valida de
nuevo el plan y devuelve una lista de resultados. Cada resultado conserva su
`intent`, `source` y `as_of_utc`; el peso diario no se suma al avance
acumulado de una OF. Cada herramienta comprueba `ASISTENTE_PRODUCCION_USAR`
además de las capacidades específicas de su servicio canónico. Las lecturas
PostgreSQL se fijan a una transacción `REPEATABLE READ, READ ONLY`; SQLite se
usa para pruebas locales.

Los identificadores y filtros se resuelven literalmente. `Azure` permanece
`Azure` y nunca se convierte de forma difusa a `Azul`. La resolución de una
manga por código usa como máximo dos filas para detectar ambigüedad sin cargar
un conjunto abierto. Las consultas de periodo admiten siete días inclusive y
500 filas totales. No se cachea la trazabilidad de mangas; el caché diario ya
está particionado por actor, permisos, fecha y versión, y conserva el corte de
fuente al servir un hit.

El endpoint coordinador debe exponer `get_catalogue()` sin permitir que el
cliente suministre su propio plan o herramienta. El campo `suggestions` tiene
objetos `{label, query}` listos para reutilizar como ejemplos.

## Revisión opcional de un plan de modelo

El gateway puede entregar una decisión de datos con forma exacta
`{status, plan}` a `review_model_plan(decision, query, now=None)`. La revisión
no interpreta texto generado ni ejecuta herramientas: comprueba slots,
entidades literales y fechas expresadas por la persona, y devuelve el plan
normalizado. Acepta una paráfrasis desconocida cuando contiene la OF, color,
fecha o manga exactos; no permite inventar un slot ausente. `OF-123` se
normaliza a `OF-000123`, mientras `Azure` conserva su identidad y no se
convierte a `Azul`.

`preflight_model_query(query)` debe invocarse antes de cualquier fallback. Su
resultado es `None` para texto seguro o una respuesta estructurada para
escritura, inyección, negación, directivas `/think`, `/model` o `/tools`, o
una entidad/fecha claramente incompleta. Las llamadas de modelo y herramientas
permanecen deshabilitadas por defecto; `get_model_catalogue()` publica el
esquema estricto y `model_routing: "disabled_by_default"`.
