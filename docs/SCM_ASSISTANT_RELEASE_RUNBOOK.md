# Prueba personal: asistente integrado sobre Avance OF TV

## Alcance y autorización

Usuario autorizó publicar para probar (Sentinel_945c47f497908191b1ffb944712f0e54). Este release conserva TV BE4c45a11d18660025a8b798b15fdc12f88ff1523d y FE7ffbc28d75022f1ac01404483ba266ca4f4f5875. No contiene reprint inacabado. Publicar ramas/CI no ejecuta migración ni despliega: infraestructura coordina después de terminar TV.

El piloto mantiene cuatro lecturas fijas; propuestas son backlog y diff manual versionado, no generador ni ejecutor de código. UX provisional, UAT humana pendiente; prueba personal no habilita uso operativo en planta.

## Configuración para interfaz visible sin llamadas al modelo

Conservar conexión/secretos actuales; no copiar tokens ni repetir login. Variables BE de la prueba (aplicar solo en el despliegue coordinado):

```dotenv
SCM_ASSISTANT_ENABLED=true
SCM_ASSISTANT_V2_ENABLED=true
SCM_ASSISTANT_PROVIDER=openclaw
SCM_ASSISTANT_V2_MODEL_CALLS_ENABLED=false
SCM_OPENCLAW_POLICY_CONFIRMED=false
SCM_ASSISTANT_ACTOR_ID=1
SCM_ASSISTANT_AUTH_USER_ID=28044442-3caf-4a92-97e4-212b50aa3c3b
```

Conservar SCM_AUTH_MODE=supabase y enlace verificado actor/JWT. No asignar roles ni capacidades a otros usuarios. El propietario tiene su sesión personal separada del rol ASISTENTE_SCM; poseer el rol no transfiere la sesión del proveedor. Capacidades de lectura requeridas: ASISTENTE_PRODUCCION_USAR, OT_VER, MANGA_PESAJE_VER; trazabilidad exige además permisos canónicos aplicables.

Build FE (variables Vite son de compilación, no basta cambiar el contenedor de un bundle ya construido):

```dotenv
VITE_SCM_ASSISTANT_ENABLED=true
VITE_SCM_ASSISTANT_V2_ENABLED=true
VITE_SCM_AUTH_MODE=supabase
```

Conservar API URL y configuración pública de autenticación existentes. No poner secretos OpenClaw en VITE. Orb oculto en TV/fullscreen. Consultas conocidas funcionan sin LLM; respuestas v2 indican provider.status=ACTIVATION_PENDING. /asistente/estado legado puede indicar AUTH_PENDING con policy=false: esto no prueba sesión desconectada ni exige nuevo OAuth. Comprobar en UI que no se anuncie inferencia activa. Consulta no resuelta puede devolver aclaración/fuera de alcance mientras IA esté pendiente.

No habilitar MODEL_CALLS ni POLICY con esta autorización. Antes de una inferencia real, pedir explícitamente: «¿Autorizas enviar una consulta mínima de prueba al gateway privado https://openclaw-scm:18789 usando tu sesión personal, con el texto de esa consulta, catálogo fijo y fecha Lima, sin resultados SCM, para comprobar modelo y esfuerzo verificados?» La aprobación previa rechazada no se presume reemplazada por publicar. Verificar primero disponibilidad/ID y esfuerzo de la ruta, sin revelar credenciales. ID/configuración general no demuestra elegibilidad de esta cuenta.

## Migración: preflight y respaldo antes de producción

Revisión nueva fd4e5f607182, padre fc3d4e5f6071. Solo crea scm_assistant_proposal y scm_assistant_proposal_revision, con FKs a trabajador y restricciones de deduplicación/versionado. No modifica stock, OF ni pesajes. Requiere privilegios DDL del operador para dos tablas/secuencia/restricciones; aplicación no migra al arrancar.

1. Registrar SHA/imagen BE y FE desplegados, revisión Alembic actual y configuración anterior sin secretos. Ejecutar `flask --app app:create_app db current` en el contenedor/contexto correcto. Si no es fc3d4e5f6071, detener y revisar cadena; no ejecutar un upgrade global a head a ciegas.
2. Confirmar respaldo PostgreSQL reciente restaurable, destino seguro fuera del contenedor, tamaño/checksum y acceso del operador. Preferir snapshot o pg_dump formato custom de la base completa mediante credenciales existentes, sin imprimir URL/contraseña. Verificar inventario con pg_restore --list y restauración en base aislada antes del cambio. Este documento no acredita que el backup exista.
3. Si alguna tabla propuesta ya existe, detener: cotejar esquema/revisión, no crear ni borrar automáticamente. Mantener v2 desactivado mientras se aplica. No modificar roles como parte de la migración.
4. Revisar SQL específico y ejecutar únicamente `flask --app app:create_app db upgrade fd4e5f607182` tras coordinación de infraestructura y respaldo verificado. Nunca stamp para saltar aplicación real.
5. Verificar revisión final, ambas tablas y constraints; confirmar permisos del actor y operaciones de propuesta de prueba. Activar flags de interfaz conservando policy/modelcalls false. Revisar que las consultas no escriban negocio.

Validación ya ejecutada: upgrade/downgrade SQLite temporal preservando trabajador y fila existente; generación DDL PostgreSQL offline comprueba exactamente dos CREATE TABLE, sin UPDATE/INSERT/ALTER TABLE de negocio. No equivale a ensayo runtime PostgreSQL productivo. CI postgres-smoke general debe revisarse por SHA; no inferir cobertura de esta migración si no la ejecuta.

## Rollback

Primera medida: desactivar SCM_ASSISTANT_V2_ENABLED y reconstruir FE sin flag v2 o volver al SHA TV verificado. Mantener POLICYfalse y MODEL_CALLSfalse. Las tablas nuevas pueden quedar sin uso: el rollback de código no requiere borrarlas.

Downgrade opcional elimina historial y propuestas. Antes, detener escrituras a propuestas y exportar ambas tablas con sus relaciones/versiones/hashes; conservar respaldo y probar restauración. Solo con decisión del operador ejecutar `flask --app app:create_app db downgrade fc3d4e5f6071` desde fd4e5f607182. No bajar revisiones previas ni eliminar otras tablas. Si hay revisiones posteriores, revisar plan de rollback antes de actuar.

## Smoke personal posterior al despliegue

- Confirmar SHAs reales y revisión DB; estado gateway pendiente con cero invocaciones nuevas.
- Actor autorizado ve orb; actor sin capacidades no lo ve y API deniega; proveedor personal no se transfiere.
- TV: tarjetas OF, siete colores simultáneos, identidad pieza, sin orb. Carga inicial mantiene identidades con métricas desconocidas.
- Primera consulta fechada explícitamente «Resumen de producción del 2026-10-05»; fuente/corte/IDs visibles. Pesajes efectivos y anulados separados; no sumar legacy ni etiquetas.
- OF/color exactos, rango acotado, trazabilidad con permisos; negación y Azure/Azul no sustituyen filtros. Sin escrituras stock/OF/pesajes.
- Propuesta de prueba solo si migración está aplicada; diff manual y aprobación no ejecutan código. No aprobar UAT humana por estas verificaciones técnicas.

## Referencias

Contratos locales: docs/SCM_ASSISTANT_CATALOGUE.md y docs/SCM_ASSISTANT_V2_DESIGN.md. Integración oficial gateway https://docs.openclaw.ai/gateway/openai-http-api ; setup https://docs.openclaw.ai/providers/openai/setup ; esfuerzo https://docs.openclaw.ai/tools/thinking ; autenticación personal https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt . Los documentos describen mecanismos, no elegibilidad particular. Sin endpoints no documentados ni shell/SQL libre para modelo.
