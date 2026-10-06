# Asistente experimental SCM

Ruta de solo lectura: GET `/api/scm/v1/asistente/estado`, POST `/api/scm/v1/asistente/consulta`. Candidato deshabilitado por defecto, sin OAuth ni despliegue automático.

## Acceso y propiedad

SCM_AUTH_MODE=supabase; JWT verificado y vinculado al trabajador activo. Requiere ASISTENTE_PRODUCCION_USAR, OT_VER y MANGA_PESAJE_VER. Rol ASISTENTE_SCM aporta únicamente la primera capacidad: reasignarlo no concede permisos de datos ni transfiere la sesión personal del proveedor.

SCM_ASSISTANT_ACTOR_ID y SCM_ASSISTANT_AUTH_USER_ID identifican al propietario del proveedor, no restringen el resumen determinístico. Si otro actor tiene la capacidad recibe PROVIDER_NOT_LINKED y no se llama al gateway. Falta vincular su proveedor propio o una API empresarial aprobada.

## Configuración

SCM_ASSISTANT_ENABLED=false y VITE_SCM_ASSISTANT_ENABLED=false por defecto. SCM_ASSISTANT_PROVIDER=deterministic u openclaw. Para el gateway privado preparado por infraestructura:

```dotenv
SCM_OPENCLAW_URL=https://openclaw-scm:18789
SCM_OPENCLAW_PRIVATE_HOST=openclaw-scm
SCM_OPENCLAW_TOKEN_FILE=/run/secrets/openclaw-token
SCM_OPENCLAW_CA_FILE=/run/openclaw/gateway-ca.crt
SCM_OPENCLAW_POLICY_CONFIRMED=false
```

Los paths son dentro del runtime del backend: comprobar destinos efectivos de mounts del override antes de activar. Token archivo prevalece sobre SCM_OPENCLAW_TOKEN; falta/archivo inválido falla cerrado. No imprimir ni incluir secretos en frontend/logs. CA personalizada se pasa a requests.verify, manteniendo hostname y TLS verificados. HTTP solo loopback; HTTPS remoto solo hostname privado explícito. No redirecciones/proxy heredado.

POST /v1/chat/completions usa agente openclaw/scm-personal, sesiones sin user compartido y tool_choice none. La política deny-all de herramientas internas se valida en infraestructura: tool_choice no la sustituye. Token gateway equivale a operador, no a credencial read-only. Agente sin herramientas/credenciales SCM. No se promete aislamiento egress absoluto.

Solo agregado fecha/zona/corte/OF/color/kg/conteos llega al proveedor; IDs y trabajadores quedan SCM. Consulta estructurada única production_daily_summary, máximo500pesajes/100grupos. Caché local TTL60s por actor/permisos/fecha/versión, permiso revalidado en cada llamada, refresh invalida actor. Cutoff conservado al reutilizar. Logs acotados en memoria, intención/patrón/latencia/uso cuando informado; no ahorro demostrado.

## Validación y operación

29 pruebas focales de servicio/HTTP verificaron autorización, separación del proveedor, fechas, pesos efectivos, anulados, caché, corte de snapshot, transporte y límites. Consulta candidata ejecutada en memoria contra SCM verificó repeatable read/read only y139pesajes efectivos1005.400kg,1anulado2.500kg para05-Oct-2026Lima, sin instalar código remoto.

Inicialización de rol realizada por operación administrativa explícitamente autorizada: capacidad148, rol19 secundario actor1, evento5725; preservado rol15 principal. Se requiere crear/asignar esa capacidad deliberadamente en otros entornos; ningún startup crea roles. El registro auditable permite administrar/reasignar con las herramientas existentes.

Pendiente: aplicación del override backend autorizado por coordinador, login usuario vía modalidad oficial elegible, smoke de inferencia sin datos y prueba end-to-end SCM. No confundir disponibilidad de configuración con autenticación comprobada. Fallo de proveedor conserva datos determinísticos.

Fuentes: https://docs.openclaw.ai/gateway/openai-http-api ; https://docs.openclaw.ai/providers/openai/setup ; https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt . Elegibilidad particular no confirmada hasta login.
