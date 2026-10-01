# SCM en Contabo: Compose Git e imagen construida fuera del VPS

Preparación local NO publicada. Rama prep/contabo-final-ci, base exacta
167154600b58f62e536fc1cf7b0764713ab0860d (optimización ya publicada).
No modifica configuración Render, dashboard ni módulo físico de pesaje. Solo API y PostgreSQL aplicativos.
No crea credenciales, proyectos, webhooks, permisos GitHub ni despliega servicios.

## Configuración Dokploy propuesta, sin secretos

Servicio existente scm-pilot; composeId gUdHzz5cr5cYSsbq84hhu;
appName envaperuscmpilot-scmpilot-cnxr9n; projectId uqV_MPCVwsGLJrRcWL_gH.

| Campo | Valor futuro |
|---|---|
| Tipo | Docker Compose, no Stack |
| Provider | Git (genérico), no GitHub App ni Raw |
| Repository URL | https://github.com/estebanSulcaInfante/envaperu-workflow.git |
| Branch | release/scm-pilot (se creará/promoverá solo después de aprobar y publicar imagen) |
| Compose Path | ./deploy/contabo/compose.release.yaml |
| SSH Key | None |
| Enable submodules | false |
| AutoDeploy | OFF |
| Domains | ninguno hasta autorizar proxy/corte |

La rama release y compose.release.yaml NO existen todavía: no guardar esta configuración esperando que
despliegue hoy. La rama local de preparación es otra cosa y no debe confundirse con release final.
No publicar la rama ni activar workflow sin aprobación del bloque final. Primer deploy manual en UI después
de los gates. Sin APIkey Dokploy, GitHubApp, webhook, runner en VPS, builds en VPS o panel expuesto a Internet.

Soporte exacto v0.30.7 verificado: SaveGitProviderCompose guarda sourceType=git, customGitUrl,
customGitBranch, composePath y customGitSSHKeyId=null; proveedor git clona HTTPS sin key con --branch.
Código: https://github.com/Dokploy/dokploy/blob/v0.30.7/apps/dokploy/components/dashboard/compose/general/generic/save-git-provider-compose.tsx
y https://github.com/Dokploy/dokploy/blob/v0.30.7/packages/server/src/utils/providers/git.ts .
Repositorio confirmado público por API anónima GitHub y página pública; main es rama predeterminada.
No necesita conceder acceso a organización ni crear una instalación GitHub para clonar por HTTPS.

## Construcción y evidencia

Workflow contabo-image.yml: solo workflow_dispatch; publish=false por defecto. Antes de construir valida
SHA completo y comprueba que el último run tests.yml correspondiente a ese SHA en main terminó success.
Eso reutiliza fast-suite y postgres-smoke existentes con sus contratos fijados; no reemplaza el gate del
workspace. Evidencia inicial informada: backend run36900452865 y workspace run36903663774 exitosos.
Confirmar vinculación workspace/backend final antes de promover cada nueva release.

Checkout separado de receta (GITHUB_SHA) y app (source_sha), sin credenciales Git persistidas. Dockerfile
dedicado copia solo app/migrations/run.py/requirements.txt, usuario10001 y Python3.12-slim por digest aprobado.
No usa Dockerfile existente ni modifica Render. .dockerignore específico reduce contexto a runtime permitido;
revisar que no haya secretos versionados dentro de app antes de la primera publicación pública.
Prueba de imagen sin red: imports Flask/Gunicorn/psycopg2 y archivos básicos. No demuestra conexión DB/Auth/S3.
Se necesita ensayo real aislado antes del corte. Sin secreto de aplicación en build args/capas.

Publicar requiere packages:write y GITHUB_TOKEN efímero; no PAT nuevo. El workflow tiene environment
scm-pilot-image: configurar protección/revisor antes de ejecutarlo; el nombre por sí solo no garantiza gate.
Su permiso packages:write se declara en el job incluso si publish=false: aprobación necesaria antes de
activarlo. GHCR propuesto: ghcr.io/estebansulcainfante/envaperu-scm-api; tags únicos sha+run+attempt.
Se publica LA MISMA imagen testeada y se registra digest, SHA fuente, SHA receta, base y run en release-image.json.
No cambia visibilidad del package. Si se aprueba público, el administrador la establece y se verifica pull
anónimo por digest antes de despliegue. Si privado, necesita handoff separado para lectura desde VPS.
Repo público NO significa imagen pública; no exponer código/metadatos en imagen sin aprobar esa elección.

Después de publicar, con digest PG inventariado y aprobado:

```sh
python deploy/contabo/render_release.py --evidence release-image.json --postgres-image 'postgres:VERSION@sha256:DIGEST_REAL' --expected-source 167154600b58f62e536fc1cf7b0764713ab0860d --output deploy/contabo/compose.release.yaml
```

El comando es plantilla: VERSION/DIGEST_REAL no son valores ejecutables. El renderer rechaza tags sueltos,
PG18 (layout diferente) y SHA inconsistente, y no sobrescribe archivos. Después revisar/commitear el manifiesto
resuelto y evidencia no secreta en release/scm-pilot mediante promoción explícita. Workflow no hace git push.
El digest no existe aún: no inventarlo ni reemplazar por latest. Primer deploy necesita confirmar en Dokploy
que el commit clonado contiene exactamente el release aprobado.

## Aislamiento y gates operativos

Compose base: API1CPU/1GiB, DB1CPU/1.5GiB, volumen PG propio, sin ports publicados, DB red internal; API egress
para SupabaseAuth/S3. Config guard inline evita montar scripts desde el checkout que Dokploy reemplaza.
API exige scm_api y SCM_AUTH_MODE=supabase; no superuser/BYPASSRLS. Credenciales todavía ausentes.
Mantener el proveedor Auth y S3 existentes; no poner estación física/pesaje en VPS ni mover dashboardgratis.

Dokploy puede adjuntar redes/labels: revisar su Compose renderizado/preview antes del primer start y comprobar
que DB NO pertenezca a dokploy-network, ingress ni red del proxy. No basta comprobar el YAML fuente.
No conectar API al proxy hasta aprobación de dominio/TLS. Mantener idle durante preparación.

PG version/extensiones, roles owner/API/backup, grants y policies FORCE RLS deben inventariarse y aprobarse.
Backup DB externo cifrado y restauración comprobada son gates; propuesta7diarios+4semanales, RPO24h/RTO4h
pendientes de aceptación/medición. BackupS3 aplazado según usuario, no implica borrar imágenes compartidas.
Rollback de imagen requiere compatibilidad de schema; tras nuevas escrituras no apuntar a DBvieja sin
reconciliación. Un único escritor durante el corte futuro.

## Un único bloque pendiente de aprobación/necesidades

1. Publicar estos archivos revisados en backend y activar workflow manual con packages:write limitado al
   repo/paquete indicado, usando token efímero. Aprobar imagen GHCR pública para pull sin nuevas credenciales,
   o elegir privada y handoff seguro de lectura. Confirmar protección del environment scm-pilot-image.
2. Autorizar preparación Git genérica del servicio existente, promoción de release tras CI y primer deploy
   manual cuando los gates pasen. No se solicita GitHubApp, webhook, APIkey, accesoorg ni abrir panelInternet.
3. Aprobar roles/credenciales DB y uso persistente S3 existente mediante entrada directa segura; concretar
   destino backup externo/retención/restauración. No enviar passwords/tokens por chat.

Dominio/corte se aprueban después: no son necesarios para publicar la imagen ni guardar Git/Compose.
Las variables SCM_API_ENV_FILE y SCM_PG_PASSWORD_FILE siguen obligatorias: deben apuntar a archivos seguros
gestionados por administrador dentro del layout persistente Dokploy; no generar secretos ni usar rutas
de otro proyecto. El manifiesto resuelto fija imágenes y conserva únicamente placeholders de esos archivos.

Estado: spec_phase tech_spec; delivery_state review; functional_validation static_only;
ux_validation not_applicable; physical_uat pending; release_constraint no_habilitar_en_planta.
Revisión independiente/bitácora del workspace pendientes del coordinador para evitar editar sus archivos.

Validación local ejecutada: 3 pruebas unittest offline OK (múltiples casos de rechazo CI/SHA/digest);
Docker Compose config --no-interpolate --no-env-resolution OK; YAML del workflow y Compose parseado con
js-yaml ya instalado, todas las acciones fijadas a SHA, solo trigger manual y publish=false comprobados;
bash -n de cada bloque run OK; git diff --check OK tras retirar espacios sobrantes. PyYAML no estaba
instalado; no se instaló y se reutilizó js-yaml. No se ejecutó build Docker, workflow GitHub, push GHCR,
pull VPS, carga funcional ni modificación Dokploy. Faltan esas verificaciones tras aprobación.
