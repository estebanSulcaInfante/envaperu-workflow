# Candidato v2: catálogo de lectura y revisión humana

Estado: local, no publicado ni desplegado. Bases producción BE6a30b812f4ded92eab496c9aba309a1284f2c4bb y FEcb62fc98fb8a17f5d33cfb2af074a4537ee489cc. No cambios a incidentes OF74, muestras de impresión, C66 o datos operativos. La nueva activación/modelcalls sigue pendiente de autorización; pruebas con mocks y bases locales.

## Contrato de lectura

POST /api/scm/v1/asistente/chat acepta únicamente query y refresh. GET /asistente/catalogo ofrece ejemplos del catálogo. Respuesta answered/needs_clarification/unsupported, plan estructurado acotado y resultados separados con fuente/corte. No devuelve probabilidad95% ni equipara semejanza de texto con confianza calibrada. Azure no equivale Azul.

Capacidades: resumen diario, avance acumulado OF/color mediante AvanceOF canónico, pesajes de hasta7días/500lecturas y manga exacta/trazabilidad. Cada servicio conserva sus permisos y unidades. No sumar daily con acumulado ni inventar metas diarias, causalidad o saldo global. Inventario/genealogía de una manga solo según secciones canónicas y permisos/almacén. No consulta global de stock nueva.

SQLAlchemy fijo y parámetros validados, máximo2lecturas por composición, snapshot PostgreSQL repeatableread/readonly y límites de lectura. Mensajes y registros son datos, no instrucciones. Sin shell/SQLlibre/herramientas internas OpenClaw. Caché diaria por actor/permisos/fecha/versión/TTL; trazabilidad no reutiliza datos de autorización de almacén antiguos. No memoria de respuestas compartida entre usuarios ni afirmación de ahorro sin medición.

## Flags y proveedor

SCM_ASSISTANT_V2_ENABLED=false y VITE_SCM_ASSISTANT_V2_ENABLED=false por defecto; mantener las otras flags productivas. SCM_ASSISTANT_V2_MODEL_CALLS_ENABLED=false independiente del gateway diario y de SCM_OPENCLAW_POLICY_CONFIRMED. No se activó ninguna en producción. Propietario actor1+UUID verificados sigue separado del rol reasignable; otra identidad no consume esa sesión.

Transporte configurable conserva agente openclaw/scm-personal, token_file servidor, CA/hostname verificados y tools none; política interna deny-all sigue obligatoria. SCM_OPENCLAW_BACKEND_MODEL vacío por defecto: si se configura exige SCM_OPENCLAW_MODEL_VERIFIED=true y usa header documentado x-openclaw-model. /v1/models enumera agentes, no prueba disponibilidad del modelo proveedor.

Sol6.1 con esfuerzo medium es candidato, NO modelo validado para esta cuenta. No se inventó un ID de proveedor. Se requiere catálogo autorizado real y smoke tras aprobación. La documentación OpenClaw admite thinkingDefault per-agent/params.thinking; configurar medium solo cuando perfil/authroute declare soporte. No enviar un campo reasoning_effort no documentado por el endpoint. No cambiar defaults productivos al preparar candidato.

Fuentes oficiales consultadas: https://docs.openclaw.ai/gateway/openai-http-api (agent-target/modeloverride); https://docs.openclaw.ai/providers/openai/setup (cuenta/autorización); https://docs.openclaw.ai/tools/thinking (esfuerzo por modelo). Documentación confirma mecanismo, no elegibilidad particular.

## Propuestas durables, sin ejecutor automático

Backlog y diff preparados manualmente. No hay generador de código operativo, autocommit, publicación ni ejecución de un diff aprobado. Necesidades se deduplican por normalización exacta por actor, sin unir temas distintos por similitud no calibrada.

Estados PENDIENTE, EN_PREPARACION, LISTA_REVISION, APROBADA, RECHAZADA. Cada edición incrementa versión y conserva snapshot histórico. Aprobar exige versión y SHA256 exacto del diff mostrado, propietario autorizado y capacidad administrativa. Cambiar diff/resumen/estado después invalida aprobación; una aprobación no equivale autorización de despliegue de futuros cambios. Concurrencia controlada con rowlock/versionoptimista y conflicto409. Sin automatización de revisión periódica: usuario revisa manualmente.

Migración NUEVA fd4e5f607182, padre fc3d4e5f6071: crea solo scm_assistant_proposal y scm_assistant_proposal_revision. No seed de permisos ni negocio. Probada local upgrade/downgrade y DDL PostgreSQL offline. No aplicada a producción; requiere coordinación previa. Respaldar/exportar revisiones antes de downgrade: este elimina tablas de propuestas. La aplicación no ejecuta create_all ni migraciones al arrancar.

## Portabilidad futura a Odoo

Conservar contrato intent/parámetros/resultados/fuentes separado de ORM. Reemplazar adaptadores canónicos SCM por servicios Odoo autorizados sin dar consultas SQL al modelo. Mapear actor a usuario/compañía verificados, traducir permisos y reglas de almacén; no reutilizar IDs numéricos entre sistemas. Revisiones y decisiones se exportan con UUID, hash de diff, versión, autor y timestamps; conservar historial append-only y aprobación ligada a contenido. La sesión personal permanece fuera de Odoo/modelos de negocio. Recalibrar métricas y unidades con fuentes reales, nunca sumar stock/legacy a partir de etiquetas. No se implementó una migración Odoo en este incremento.

## UAT pendiente

Esfera/panel: dragmouse/tacto y snapbordes, clickseparado, keyboardbutton/escape local, minimizar/reabrir, coordenadas poractor/dispositivo, safeareas, sin foco automático/globalEnter y oculto TV/fullscreen. Revisar que escaneo/pesaje C66 conserva foco y Enter; probar narrow390px/desktop. Estados conexiónpendiente/consultando/error fieles. Referencias Library inspeccionadas como inspiración visual, no prueba de conexiones actuales. UXprovisional; no habilitar planta sin validación humana.


## Verificación del candidato

El endpoint v2 conecta el adaptador `propose_read_plan` para lenguaje no resuelto por el router de preguntas frecuentes. Requiere propietario personal, proveedor openclaw, SCM_ASSISTANT_V2_MODEL_CALLS_ENABLED=true, política confirmada y modelo verificado. Todo permanece apagado por defecto; pruebas con mocks, sin inferencia real. El modelo devuelve solamente status+plan; un validador independiente verifica catálogo, entidades/fechas literales, negaciones y composición explícita antes de ejecutar servicios fijos. Los estados needs_clarification/unsupported y fallos no ejecutan lecturas. Los datos SCM resultantes no se envían al modelo. No se anuncia generador de código ni autonomía operativa.

Todas las cargas ORM de la sesión dedicada tienen presupuesto: máximo 500 entidades por lectura, 4.000 filas acumuladas y 200 consultas internas por plan, más timeout PostgreSQL de 5 segundos por sentencia y respuesta máxima de 1 MB por resultado. El presupuesto incluye cargas de relaciones canónicas y falla explícitamente; no devuelve un informe truncado como completo. La trazabilidad canónica utiliza siempre la sesión recibida. Su campo `as_of` indica tiempo de proyección; el envoltorio `as_of_utc` conserva el corte transaccional. La caché diaria mantiene su propio corte original.

Historial de propuestas: páginas de 20 versiones, más recientes primero, con cursor before_version y has_more. No se elimina historia al paginar. Registro de consultas local acotado en memoria del proceso por actor, consulta, plan, resultado, latencia y uso nulo si no medible; no persiste respuestas viejas ni sobrevive reinicio. Propuestas sí se persisten en las tablas nuevas.


## Router LLM y modelo/esfuerzo (verificado documentalmente 2026-10-06)

OpenAI confirma GPT-6.1 Sol, ID API `gpt-6.1-sol`, y evaluaciones con esfuerzo medium: https://openai.com/index/introducing-gpt-6-1-sol/ . Esto acredita el modelo general, no su presencia en el catálogo de la cuenta esulca ni la ruta concreta de OpenClaw. No se consultaron credenciales ni catálogo privado en este incremento.

Modelo configurable por SCM_OPENCLAW_BACKEND_MODEL (sin valor predeterminado inventado), verificación SCM_OPENCLAW_MODEL_VERIFIED. El header x-openclaw-model está documentado: https://docs.openclaw.ai/gateway/openai-http-api . El endpoint /v1/models lista agentes y no valida disponibilidad de modelos proveedor. El ID exacto de catálogo deberá verificarse antes de activar; no se infiere de nombres comerciales.

Esfuerzo opcional SCM_OPENCLAW_THINKING_LEVEL y SCM_OPENCLAW_THINKING_VERIFIED. Solo se admite un nivel conocido y verificado para la ruta elegida. Se usa directiva documentada /think:<nivel> en la solicitud sin sesión persistente, no campos HTTP reasoning_effort inventados. Fuente: https://docs.openclaw.ai/tools/thinking . Alternativamente dejarlo vacío y configurar el default del agente con params.thinking/ thinkingDefault fuera de esta aplicación. No se configura medium en producción automáticamente. Comandos /think, /model y otras directivas dentro de la consulta del usuario se rechazan antes de enviar al gateway.

Cuatro capacidades exactas: (1) pesajes efectivos/anulados de una fecha Lima, agrupados OF/color; (2) avance acumulado OF/color canónico y su cobertura, sin meta diaria inventada; (3) pesajes en rango explícito hasta7días/500filas; (4) detalle/trazabilidad de una manga con permisos de inventario/genealogía existentes. Stock global, escrituras, causalidad y generación de código quedan fuera.

Contenido de propuestas: PENDIENTE puede contener solo necesidad/backlog; EN_PREPARACION contiene resumen/diff que una persona prepara manualmente; LISTA_REVISION exige resumen y diff exacto no vacío. APROBADA/RECHAZADA registran decisión versionada. Ninguna aprobación aplica el patch. Un generador de código aislado y su ejecutor requieren una fase posterior explícita.

El clasificador transmite la consulta literal del propietario, catálogo fijo y fecha Lima; no transmite resultados SCM. Una llamada como máximo por petición, sin reintentos automáticos ni bucle agéntico; salida600tokens/4000caracteres, timeout20s y límites de transporte existentes. El consumo informado proviene de usage cuando está disponible. No se afirma ahorro medido. El piloto no incorpora cuota monetaria mensual; mantener activación supervisada y definir presupuesto de uso antes de abrir alcance a más usuarios.
