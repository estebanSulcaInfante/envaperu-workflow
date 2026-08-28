from dataclasses import dataclass

from app.extensions import db
from app.models.scm_catalogos import (
    MODALIDAD_POR_CONFIGURAR,
    MODALIDAD_SEGUNDA,
    MODALIDAD_VIRGEN,
    ScmCapacidad,
    ScmCategoriaRecepcion,
)
from app.models.scm_reproceso import (
    ScmCondicionMerma,
    ScmFamiliaMaterialReproceso,
    ScmProcesoMaterialReproceso,
    ScmReglaCompatibilidadReproceso,
)
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_alert_service import seed_alert_rule


CAPACIDADES_SCM_INICIALES = (
    ("PROVEEDOR_ADMINISTRAR", "Administrar proveedores"),
    (
        "DOCUMENTO_PROVEEDOR_REGISTRAR",
        "Registrar documentos externos de proveedor",
    ),
    ("OC_CREAR", "Crear órdenes de compra de material"),
    ("OC_APROBAR", "Aprobar órdenes de compra de material"),
    ("RECEPCION_CONFIRMAR", "Confirmar recepciones de material"),
    (
        "ENTRADA_EXCEPCIONAL_REGULARIZAR",
        "Regularizar entradas excepcionales",
    ),
    ("CALIDAD_RESOLVER", "Resolver decisiones de Calidad"),
    (
        "LIBERACION_DIRECTA_ADMINISTRAR",
        "Administrar políticas de liberación directa",
    ),
    ("CORRECCION_SOLICITAR", "Solicitar correcciones de recepción"),
    ("CORRECCION_APROBAR", "Aprobar correcciones de recepción"),
    ("DEVOLUCION_REGISTRAR", "Registrar devoluciones a proveedor"),
    (
        "CONFIG_RECEPCION_ADMINISTRAR",
        "Administrar configuración de recepción",
    ),
    ("AUTORIZACION_SCM_ADMINISTRAR", "Administrar autorizaciones SCM"),
    ("ARTICULO_VER", "Consultar artículos SCM"),
    ("ARTICULO_ADMINISTRAR", "Administrar artículos SCM"),
    ("ESTRUCTURA_VER", "Consultar estructuras de producto"),
    ("ESTRUCTURA_ADMINISTRAR", "Administrar borradores de estructura"),
    ("ESTRUCTURA_APROBAR", "Aprobar estructuras"),
    (
        "ESTRUCTURA_PUBLICAR_DIRECTO",
        "Publicar estructuras directamente como jefatura",
    ),
    ("RUTA_VER", "Consultar rutas de producción"),
    ("RUTA_ADMINISTRAR", "Administrar borradores de ruta"),
    ("RUTA_APROBAR", "Aprobar rutas de producción"),
    (
        "RUTA_PUBLICAR_DIRECTO",
        "Publicar rutas directamente como jefatura",
    ),
    ("EMPAQUE_VER", "Consultar perfiles y reglas de empaque"),
    ("EMPAQUE_ADMINISTRAR", "Administrar empaque"),
    ("EMPAQUE_APROBAR", "Aprobar reglas de empaque"),
    (
        "EMPAQUE_PUBLICAR_DIRECTO",
        "Publicar reglas de empaque directamente como jefatura",
    ),
    ("OPERACION_PLANIFICAR", "Planificar operaciones"),
    ("OPERACION_EJECUTAR", "Ejecutar operaciones"),
    ("OPERACION_CORREGIR", "Corregir operaciones"),
    ("WIP_VER", "Consultar WIP y genealogía"),
    ("WIP_LIBERAR", "Liberar WIP por Calidad"),
    ("TIPO_MANGA_ADMINISTRAR", "Administrar tipos de manga"),
    ("OT_VER", "Consultar órdenes de trabajo"),
    ("OT_CREAR", "Crear órdenes de trabajo"),
    ("OT_INICIAR", "Iniciar órdenes de trabajo"),
    ("OT_CERRAR", "Cerrar órdenes de trabajo"),
    ("PLAN_MANGA_VER", "Consultar planes de manga"),
    ("PLAN_MANGA_ADMINISTRAR", "Administrar planes de manga"),
    ("MANGA_PLANIFICAR", "Planificar mangas"),
    ("MANGA_ANULAR", "Anular mangas"),
    ("MANGA_EXTRA_SOLICITAR", "Solicitar mangas extra"),
    ("MANGA_EXTRA_APROBAR", "Aprobar mangas extra"),
    ("MANGA_ETIQUETA_PRE_GENERAR", "Generar etiqueta prepesaje"),
    (
        "MANGA_ETIQUETA_REEMPLAZAR_SOLICITAR",
        "Solicitar reemplazo de etiqueta de manga",
    ),
    (
        "MANGA_ETIQUETA_REEMPLAZAR_APROBAR",
        "Aprobar reemplazo de etiqueta de manga",
    ),
    ("MANGA_PESAR", "Confirmar pesaje de manga"),
    (
        "MANGA_CONTROL_PESO_REGISTRAR",
        "Registrar un corte acumulado de una manga que continua abierta",
    ),
    (
        "MANGA_TRANSFERIR_OT",
        "Vincular una manga abierta a una OT compatible",
    ),
    ("MANGA_PESAJE_VER", "Consultar pesajes de manga"),
    (
        "MANGA_ETIQUETA_POST_IMPRIMIR",
        "Imprimir etiqueta final de pesaje",
    ),
    ("PESAJE_CORRECCION_SOLICITAR", "Solicitar corrección de pesaje"),
    ("PESAJE_CORRECCION_APROBAR", "Aprobar corrección de pesaje"),
    ("ANULAR_PESAJE", "Anular un pesaje SCM de forma controlada"),
    ("PESAJE_TARA_OVERRIDE", "Autorizar tara distinta del snapshot"),
    ("OP_VER", "Consultar ordenes de produccion"),
    ("OP_CREAR", "Crear ordenes de produccion"),
    ("OP_APROBAR", "Aprobar ordenes de produccion"),
    ("OP_CANCELAR", "Cancelar ordenes de produccion no planificadas"),
    ("PLANIFICACION_CALCULAR", "Calcular cobertura y propuestas"),
    ("PLANIFICACION_CONFIRMAR", "Confirmar el plan de suministro"),
    ("INVENTARIO_VER", "Consultar Kardex normalizado"),
    ("INVENTARIO_SALDO_INICIAL", "Registrar saldos iniciales"),
    ("INVENTARIO_AJUSTAR", "Registrar ajustes auditados de inventario"),
    ("ALMACEN_CONFIG_ADMINISTRAR", "Administrar almacenes y ubicaciones"),
    ("ALMACEN_SCOPE_ADMINISTRAR", "Administrar alcance de almacenes por trabajador"),
    ("INVENTARIO_MOVILIZAR", "Ejecutar movimientos entre ubicaciones"),
    ("INVENTARIO_CONTROL_TRANSVERSAL", "Consultar inventario de todos los almacenes"),
    ("INVENTARIO_APERTURA_PREPARAR", "Preparar lotes de apertura inicial"),
    ("INVENTARIO_APERTURA_APROBAR", "Aprobar lotes de apertura inicial"),
    ("MATERIAL_REQUERIMIENTO_GENERAR", "Generar requerimientos de material de una OF"),
    ("MATERIAL_RESERVAR", "Reservar material para una OF"),
    ("MATERIAL_EMITIR", "Emitir material reservado a Produccion"),
    ("MATERIAL_DEVOLVER", "Devolver material emitido al almacen"),
    ("MATERIAL_PREMEZCLA_CONFIRMAR", "Confirmar transformacion de premezcla"),
    ("RECEPCION_MANGA_VER", "Consultar recepcion de mangas"),
    ("RECEPCION_MANGA_CONFIRMAR", "Confirmar ingreso de mangas al almacen"),
    ("RECEPCION_MANGA_RECHAZAR", "Rechazar mangas antes de recibirlas"),
    ("RECEPCION_MANGA_REVERSION_SOLICITAR", "Solicitar reversa de una recepcion de manga"),
    ("RECEPCION_MANGA_REVERSION_APROBAR", "Aprobar reversa compensatoria de una recepcion"),
    (
        "RECEPCION_MANGA_BUSCAR_MANUAL",
        "Buscar mangas manualmente durante la recepcion",
    ),
    ("CALIDAD_MANGA_VER", "Consultar mangas pendientes de calidad"),
    ("CALIDAD_MANGA_LIBERAR", "Liberar existencias de manga"),
    ("CALIDAD_MANGA_BLOQUEAR", "Bloquear existencias de manga"),
    ("CALIDAD_MANGA_RECHAZAR", "Rechazar existencias de manga"),
    ("ABASTECIMIENTO_VER", "Consultar abastecimiento interno"),
    ("ABASTECIMIENTO_SOLICITAR", "Solicitar componentes para una OT de Armado"),
    ("PICKING_PREPARAR", "Reservar y preparar mangas por QR"),
    ("PICKING_DESPACHAR", "Despachar picking hacia Produccion"),
    ("ABASTECIMIENTO_RECIBIR", "Recibir picking en Mesa de Armado"),
    ("ABASTECIMIENTO_DEVOLVER", "Devolver remanentes desde Armado"),
    ("RETORNO_RECIBIR", "Recibir remanentes en Almacen"),
    ("UNIDAD_LOGISTICA_FRACCIONAR", "Autorizar fraccionamiento fisico de una manga"),
    ("GENEALOGIA_CANDIDATA_CONFIRMAR", "Confirmar genealogia por candidatos"),
    ("GENEALOGIA_LEGACY_APERTURA", "Autorizar apertura de stock legacy contado"),
    ("GENEALOGIA_VER", "Consultar genealogia exacta de mangas"),
    ("ENSAMBLE_PLANIFICAR", "Planificar mangas de salida de Armado"),
    ("ENSAMBLE_MANGA_CERRAR", "Confirmar cantidad y consumos de una manga de Armado"),
    ("ENSAMBLE_CORREGIR_SOLICITAR", "Solicitar correccion de cantidad de Armado"),
    ("ENSAMBLE_CORREGIR_APROBAR", "Aprobar correccion compensatoria de Armado"),
    ("ABASTECIMIENTO_CORREGIR_SOLICITAR", "Solicitar correccion de abastecimiento"),
    ("ABASTECIMIENTO_CORREGIR_APROBAR", "Aprobar correccion de abastecimiento"),
    ("ABASTECIMIENTO_EMERGENCIA_APROBAR", "Aprobar abastecimiento no planificado"),
    ("OF_VER", "Consultar ordenes de fabricacion"),
    ("OF_EDITAR_BORRADOR", "Editar borradores de fabricacion"),
    ("OF_EXCEPCIONAL_CREAR", "Crear fabricacion excepcional"),
    ("OF_LIBERAR", "Liberar ordenes de fabricacion"),
    ("OF_ANULAR", "Anular ordenes de fabricacion"),
    ("OA_VER", "Consultar ordenes de armado"),
    ("OA_LIBERAR", "Liberar ordenes de armado"),
    ("OA_EJECUTAR", "Ejecutar y cerrar ordenes de armado"),
    ("OA_ANULAR", "Anular ordenes de armado"),
    ("MERMA_RECUPERABLE_REGISTRAR", "Registrar y pesar merma recuperable"),
    ("MOLIENDA_VER", "Consultar merma, molienda y genealogia"),
    ("MOLIENDA_ORDEN_CREAR", "Crear y preparar ordenes de molienda"),
    ("MOLIENDA_EJECUTAR", "Pesar, ejecutar y cerrar molienda"),
    ("MOLIENDA_REGLA_ADMINISTRAR", "Administrar maestros y reglas de reproceso"),
    ("MOLIENDA_REGLA_APROBAR", "Aprobar reglas de compatibilidad"),
    ("MOLIENDA_EXCEPCION_APROBAR", "Autorizar excepciones de molienda"),
    ("MOLIENDA_LOTE_LIBERAR", "Liberar material recuperado"),
    ("MOLIENDA_ANULAR", "Anular ordenes y lotes de molienda"),
    ("ALERTA_VER", "Consultar alertas operativas"),
    ("ALERTA_GESTIONAR", "Reconocer, asignar y cerrar alertas"),
    ("ALERTA_CONFIGURAR", "Administrar reglas y umbrales de alerta"),
)


_VISTAS_PRODUCCION = (
    "ARTICULO_VER",
    "ESTRUCTURA_VER",
    "RUTA_VER",
    "EMPAQUE_VER",
    "OT_VER",
    "PLAN_MANGA_VER",
    "MANGA_PESAJE_VER",
    "WIP_VER",
    "OP_VER",
    "OF_VER",
    "OA_VER",
        "INVENTARIO_VER",
        "INVENTARIO_CONTROL_TRANSVERSAL",
    "MOLIENDA_VER",
    "ALERTA_VER",
    "RECEPCION_MANGA_VER",
    "CALIDAD_MANGA_VER",
    "ABASTECIMIENTO_VER",
)


ROLES_SCM_INICIALES = (
    (
        "GERENTE_GENERAL",
        "Gerente General",
        tuple(
            codigo
            for codigo, _nombre in CAPACIDADES_SCM_INICIALES
        ),
    ),
    (
        "COMPRAS",
        "Compras",
        (
            "PROVEEDOR_ADMINISTRAR",
            "DOCUMENTO_PROVEEDOR_REGISTRAR",
            "OC_CREAR",
        ),
    ),
    (
        "ALMACEN_RECEPCION",
        "Almacén / Recepción",
        (
            "RECEPCION_CONFIRMAR",
            "DOCUMENTO_PROVEEDOR_REGISTRAR",
            "CORRECCION_SOLICITAR",
            "DEVOLUCION_REGISTRAR",
            "INVENTARIO_VER",
            "INVENTARIO_MOVILIZAR",
            "INVENTARIO_APERTURA_PREPARAR",
            "MATERIAL_EMITIR",
            "MATERIAL_DEVOLVER",
            "MERMA_RECUPERABLE_REGISTRAR",
            "MOLIENDA_VER",
            "ALERTA_VER",
            "RECEPCION_MANGA_VER",
            "RECEPCION_MANGA_CONFIRMAR",
            "RECEPCION_MANGA_RECHAZAR",
            "RECEPCION_MANGA_REVERSION_SOLICITAR",
            "RECEPCION_MANGA_BUSCAR_MANUAL",
            "ABASTECIMIENTO_VER",
            "PICKING_PREPARAR",
            "PICKING_DESPACHAR",
            "RETORNO_RECIBIR",
        ),
    ),
    (
        "CALIDAD",
        "Calidad",
        (
            "CALIDAD_RESOLVER",
            "ARTICULO_VER",
            "ESTRUCTURA_VER",
            "RUTA_VER",
            "WIP_VER",
            "WIP_LIBERAR",
            "RECEPCION_MANGA_VER",
            "CALIDAD_MANGA_VER",
            "CALIDAD_MANGA_LIBERAR",
            "CALIDAD_MANGA_BLOQUEAR",
            "CALIDAD_MANGA_RECHAZAR",
        ),
    ),
    (
        "GERENCIA",
        "Gerencia",
        (
            "OC_APROBAR",
            "CORRECCION_APROBAR",
            "AUTORIZACION_SCM_ADMINISTRAR",
            "OP_APROBAR",
            "ESTRUCTURA_ADMINISTRAR",
            "ESTRUCTURA_PUBLICAR_DIRECTO",
            "RUTA_ADMINISTRAR",
            "RUTA_PUBLICAR_DIRECTO",
            "EMPAQUE_ADMINISTRAR",
            "EMPAQUE_PUBLICAR_DIRECTO",
            "ALERTA_VER",
            *_VISTAS_PRODUCCION,
        ),
    ),
    (
        "SUPERVISOR",
        "Supervisor",
        (
            "ENTRADA_EXCEPCIONAL_REGULARIZAR",
            *_VISTAS_PRODUCCION,
            "OT_CREAR",
            "OT_INICIAR",
            "OT_CERRAR",
            "PLAN_MANGA_ADMINISTRAR",
            "MANGA_PLANIFICAR",
            "MANGA_EXTRA_SOLICITAR",
            "MANGA_ETIQUETA_PRE_GENERAR",
            "MANGA_ETIQUETA_REEMPLAZAR_SOLICITAR",
            "MANGA_CONTROL_PESO_REGISTRAR",
            "MANGA_TRANSFERIR_OT",
            "OPERACION_EJECUTAR",
            "OA_EJECUTAR",
            "PESAJE_CORRECCION_SOLICITAR",
            "MOLIENDA_VER",
            "MOLIENDA_ORDEN_CREAR",
            "ALERTA_VER",
            "ABASTECIMIENTO_VER",
            "ABASTECIMIENTO_SOLICITAR",
            "ABASTECIMIENTO_RECIBIR",
            "ABASTECIMIENTO_DEVOLVER",
            "ENSAMBLE_CORREGIR_SOLICITAR",
        ),
    ),
    (
        "CONFIGURACION_SCM",
        "Configuración SCM",
        (
            "CONFIG_RECEPCION_ADMINISTRAR",
            "LIBERACION_DIRECTA_ADMINISTRAR",
            "ARTICULO_VER",
            "ARTICULO_ADMINISTRAR",
            "EMPAQUE_VER",
            "EMPAQUE_ADMINISTRAR",
            "TIPO_MANGA_ADMINISTRAR",
            "MOLIENDA_VER",
            "MOLIENDA_REGLA_ADMINISTRAR",
            "ALERTA_VER",
            "ALERTA_CONFIGURAR",
            "INVENTARIO_VER",
            "ALMACEN_CONFIG_ADMINISTRAR",
            "ALMACEN_SCOPE_ADMINISTRAR",
            "INVENTARIO_CONTROL_TRANSVERSAL",
        ),
    ),
    (
        "AUDITORIA_CONSULTA",
        "Auditoría / Consulta",
        _VISTAS_PRODUCCION,
    ),
    (
        "PLANIFICACION",
        "Planificación",
        (
            "ARTICULO_VER",
            "ESTRUCTURA_VER",
            "RUTA_VER",
            "EMPAQUE_VER",
            "OPERACION_PLANIFICAR",
            "OP_VER",
            "OF_VER",
            "OA_VER",
            "OP_CREAR",
            "PLANIFICACION_CALCULAR",
            "PLANIFICACION_CONFIRMAR",
            "INVENTARIO_VER",
            "OT_VER",
            "OT_CREAR",
            "PLAN_MANGA_VER",
            "PLAN_MANGA_ADMINISTRAR",
            "MANGA_PLANIFICAR",
            "WIP_VER",
            "ABASTECIMIENTO_VER",
        ),
    ),
    (
        "INGENIERIA_SCM",
        "Ingeniería SCM",
        (
            "ARTICULO_VER",
            "ARTICULO_ADMINISTRAR",
            "ESTRUCTURA_VER",
            "ESTRUCTURA_ADMINISTRAR",
            "RUTA_VER",
            "RUTA_ADMINISTRAR",
            "EMPAQUE_VER",
            "EMPAQUE_ADMINISTRAR",
            "TIPO_MANGA_ADMINISTRAR",
            "MOLIENDA_VER",
            "MOLIENDA_REGLA_ADMINISTRAR",
            "ALERTA_VER",
            "ALERTA_CONFIGURAR",
        ),
    ),
    (
        "JEFE_PRODUCCION",
        "Jefe de Producción",
        (
            *_VISTAS_PRODUCCION,
            "ESTRUCTURA_ADMINISTRAR",
            "ESTRUCTURA_APROBAR",
            "ESTRUCTURA_PUBLICAR_DIRECTO",
            "RUTA_ADMINISTRAR",
            "RUTA_APROBAR",
            "RUTA_PUBLICAR_DIRECTO",
            "EMPAQUE_APROBAR",
            "EMPAQUE_ADMINISTRAR",
            "EMPAQUE_PUBLICAR_DIRECTO",
            "OT_CREAR",
            "OT_INICIAR",
            "OT_CERRAR",
            "PLAN_MANGA_ADMINISTRAR",
            "MANGA_PLANIFICAR",
            "MANGA_ANULAR",
            "MANGA_EXTRA_APROBAR",
            "MANGA_ETIQUETA_PRE_GENERAR",
            "MANGA_ETIQUETA_REEMPLAZAR_APROBAR",
            "MANGA_CONTROL_PESO_REGISTRAR",
            "MANGA_TRANSFERIR_OT",
            "OPERACION_PLANIFICAR",
            "OPERACION_EJECUTAR",
            "OPERACION_CORREGIR",
            "PESAJE_CORRECCION_APROBAR",
            "ANULAR_PESAJE",
            "RECEPCION_MANGA_REVERSION_APROBAR",
            "PESAJE_TARA_OVERRIDE",
            "OF_EDITAR_BORRADOR",
            "OF_EXCEPCIONAL_CREAR",
            "OF_LIBERAR",
            "OF_ANULAR",
            "OA_LIBERAR",
            "OA_EJECUTAR",
            "OA_ANULAR",
            "INVENTARIO_AJUSTAR",
            "INVENTARIO_APERTURA_APROBAR",
            "MATERIAL_REQUERIMIENTO_GENERAR",
            "MATERIAL_RESERVAR",
            "MATERIAL_PREMEZCLA_CONFIRMAR",
            "MERMA_RECUPERABLE_REGISTRAR",
            "MOLIENDA_ORDEN_CREAR",
            "MOLIENDA_EJECUTAR",
            "MOLIENDA_REGLA_ADMINISTRAR",
            "MOLIENDA_REGLA_APROBAR",
            "MOLIENDA_EXCEPCION_APROBAR",
            "MOLIENDA_LOTE_LIBERAR",
            "MOLIENDA_ANULAR",
            "ALERTA_VER",
            "ALERTA_GESTIONAR",
            "ALERTA_CONFIGURAR",
            "ABASTECIMIENTO_SOLICITAR",
            "PICKING_PREPARAR",
            "PICKING_DESPACHAR",
            "ABASTECIMIENTO_RECIBIR",
            "ABASTECIMIENTO_DEVOLVER",
            "RETORNO_RECIBIR",
            "UNIDAD_LOGISTICA_FRACCIONAR",
            "GENEALOGIA_CANDIDATA_CONFIRMAR",
            "GENEALOGIA_LEGACY_APERTURA",
            "GENEALOGIA_VER",
            "ENSAMBLE_PLANIFICAR",
            "ENSAMBLE_MANGA_CERRAR",
            "ENSAMBLE_CORREGIR_SOLICITAR",
            "ENSAMBLE_CORREGIR_APROBAR",
            "ABASTECIMIENTO_CORREGIR_SOLICITAR",
            "ABASTECIMIENTO_CORREGIR_APROBAR",
            "ABASTECIMIENTO_EMERGENCIA_APROBAR",
        ),
    ),
    (
        "JEFE_ENSAMBLE",
        "Jefe de Armado",
        (
            *_VISTAS_PRODUCCION,
            "ESTRUCTURA_ADMINISTRAR",
            "ESTRUCTURA_PUBLICAR_DIRECTO",
            "RUTA_ADMINISTRAR",
            "RUTA_PUBLICAR_DIRECTO",
            "EMPAQUE_ADMINISTRAR",
            "EMPAQUE_PUBLICAR_DIRECTO",
            "OT_CREAR",
            "OT_INICIAR",
            "OT_CERRAR",
            "OA_VER",
            "OA_EJECUTAR",
            "ABASTECIMIENTO_SOLICITAR",
            "ABASTECIMIENTO_RECIBIR",
            "ABASTECIMIENTO_DEVOLVER",
            "GENEALOGIA_CANDIDATA_CONFIRMAR",
            "GENEALOGIA_VER",
            "ENSAMBLE_PLANIFICAR",
            "ENSAMBLE_MANGA_CERRAR",
            "ENSAMBLE_CORREGIR_SOLICITAR",
            "ABASTECIMIENTO_CORREGIR_SOLICITAR",
        ),
    ),
    (
        "MAQUINISTA",
        "Maquinista",
        (
            "OT_VER",
            "MANGA_PESAR",
            "MANGA_CONTROL_PESO_REGISTRAR",
            "MANGA_PESAJE_VER",
            "WIP_VER",
        ),
    ),
    (
        "OPERADOR_MOLINO",
        "Operador de Molino",
        (
            "MOLIENDA_VER",
            "MOLIENDA_ORDEN_CREAR",
            "MOLIENDA_EJECUTAR",
            "ALERTA_VER",
        ),
    ),
    (
        "OPERADOR_PESAJE",
        "Operador de Pesaje",
        (
            "OT_VER",
            "MANGA_PESAR",
            "MANGA_CONTROL_PESO_REGISTRAR",
            "MANGA_PESAJE_VER",
            "MANGA_ETIQUETA_POST_IMPRIMIR",
            "MANGA_ETIQUETA_REEMPLAZAR_SOLICITAR",
            "PESAJE_CORRECCION_SOLICITAR",
            "WIP_VER",
        ),
    ),
)


CATEGORIAS_RECEPCION_INICIALES = (
    (
        "RESINA_VIRGEN",
        "Resina virgen",
        MODALIDAD_VIRGEN,
        False,
        True,
    ),
    (
        "RESINA_SEGUNDA",
        "Resina de segunda",
        MODALIDAD_SEGUNDA,
        False,
        True,
    ),
    (
        "LEGACY_POR_CONFIGURAR",
        "Legacy por configurar",
        MODALIDAD_POR_CONFIGURAR,
        False,
        False,
    ),
)


@dataclass(frozen=True)
class ScmSeedResult:
    capacidades_creadas: int
    roles_creados: int
    categorias_creadas: int
    relaciones_creadas: int


def ensure_initial_scm_configuration():
    """Crea configuración técnica faltante sin asignarla a trabajadores."""
    capacidades = {
        item.codigo: item for item in db.session.scalars(db.select(ScmCapacidad))
    }
    capacidades_creadas = 0
    for codigo, nombre in CAPACIDADES_SCM_INICIALES:
        if codigo in capacidades:
            continue
        capacidad = ScmCapacidad(codigo=codigo, nombre=nombre)
        db.session.add(capacidad)
        capacidades[codigo] = capacidad
        capacidades_creadas += 1

    roles = {
        item.codigo: item for item in db.session.scalars(db.select(RolOperativo))
    }
    roles_creados = 0
    relaciones_creadas = 0
    for codigo, nombre, codigos_capacidad in ROLES_SCM_INICIALES:
        rol = roles.get(codigo)
        if rol is None:
            rol = RolOperativo(codigo=codigo, nombre=nombre, activo=True)
            db.session.add(rol)
            roles[codigo] = rol
            roles_creados += 1

        codigos_existentes = {
            capacidad.codigo for capacidad in rol.capacidades
        }
        for codigo_capacidad in codigos_capacidad:
            if codigo_capacidad in codigos_existentes:
                continue
            rol.capacidades.append(capacidades[codigo_capacidad])
            codigos_existentes.add(codigo_capacidad)
            relaciones_creadas += 1

    categorias = {
        item.codigo: item
        for item in db.session.scalars(db.select(ScmCategoriaRecepcion))
    }
    categorias_creadas = 0
    for (
        codigo,
        nombre,
        modalidad,
        lote_externo_obligatorio,
        recepcion_habilitada,
    ) in CATEGORIAS_RECEPCION_INICIALES:
        if codigo in categorias:
            continue
        categoria = ScmCategoriaRecepcion(
            codigo=codigo,
            nombre=nombre,
            modalidad_default=modalidad,
            lote_externo_obligatorio=lote_externo_obligatorio,
            recepcion_habilitada=recepcion_habilitada,
            activo=True,
        )
        db.session.add(categoria)
        categorias[codigo] = categoria
        categorias_creadas += 1

    ubicaciones = {
        item.codigo: item
        for item in db.session.scalars(db.select(ScmUbicacionInventario))
    }
    ubicaciones_iniciales = (
        (
            "RECEPCION_PIEZAS_WIP",
            "Recepcion de piezas y WIP",
            ["PIEZA_COLOR", "SUBENSAMBLE_WIP"],
        ),
        (
            "RECEPCION_PT",
            "Recepcion de producto terminado",
            ["PRODUCTO_TERMINADO"],
        ),
        ("ALMACEN_GENERAL", "Almacen general", []),
    )
    for codigo, nombre, clases_articulo in ubicaciones_iniciales:
        ubicacion = ubicaciones.get(codigo)
        if ubicacion is None:
            db.session.add(ScmUbicacionInventario(
                codigo=codigo,
                nombre=nombre,
                clases_articulo_json=clases_articulo,
                activo=True,
            ))
            continue
        ubicacion.nombre = nombre
        ubicacion.clases_articulo_json = clases_articulo
        ubicacion.activo = True

    familias_reproceso = {
        item.codigo: item
        for item in db.session.scalars(db.select(ScmFamiliaMaterialReproceso))
    }
    if "PP" not in familias_reproceso:
        familias_reproceso["PP"] = ScmFamiliaMaterialReproceso(
            codigo="PP",
            nombre="Polipropileno",
            descripcion="Familia inicial configurable para el piloto.",
        )
        db.session.add(familias_reproceso["PP"])

    procesos_reproceso = {
        item.codigo: item
        for item in db.session.scalars(db.select(ScmProcesoMaterialReproceso))
    }
    for codigo, nombre in (
        ("INYECCION", "Inyeccion"),
        ("SOPLADO", "Soplado"),
    ):
        if codigo not in procesos_reproceso:
            procesos_reproceso[codigo] = ScmProcesoMaterialReproceso(
                codigo=codigo,
                nombre=nombre,
            )
            db.session.add(procesos_reproceso[codigo])

    condiciones = {
        item.codigo: item
        for item in db.session.scalars(db.select(ScmCondicionMerma))
    }
    for codigo, nombre, recuperable in (
        ("LIMPIA", "Limpia y segregada", True),
        ("CONTAMINADA", "Contaminada", False),
        ("QUEMADA", "Material quemado o transmutado", False),
    ):
        if codigo not in condiciones:
            condiciones[codigo] = ScmCondicionMerma(
                codigo=codigo,
                nombre=nombre,
                recuperable=recuperable,
            )
            db.session.add(condiciones[codigo])

    db.session.flush()
    seed_actor = db.session.scalar(
        db.select(Trabajador)
        .where(Trabajador.activo.is_(True))
        .order_by(Trabajador.id)
    )
    if seed_actor is not None:
        existing_compatibility = {
            item.codigo
            for item in db.session.scalars(
                db.select(ScmReglaCompatibilidadReproceso)
            )
        }
        pp = familias_reproceso["PP"]
        injection = procesos_reproceso["INYECCION"]
        blow = procesos_reproceso["SOPLADO"]
        initial_rules = (
            (
                "PP_INYECCION_MISMO_PROCESO",
                "PP de inyeccion con PP de inyeccion",
                injection.id,
                injection.id,
                "COMPATIBLE",
                None,
                False,
            ),
            (
                "PP_SOPLADO_MISMO_PROCESO",
                "PP de soplado con PP de soplado",
                blow.id,
                blow.id,
                "COMPATIBLE",
                None,
                False,
            ),
            (
                "PP_INYECCION_SOPLADO_10",
                "Dilucion controlada entre inyeccion y soplado",
                injection.id,
                blow.id,
                "CONDICIONADA",
                10,
                True,
            ),
        )
        for (
            codigo,
            nombre,
            proceso_objetivo_id,
            proceso_aporte_id,
            resultado,
            porcentaje,
            simetrica,
        ) in initial_rules:
            if codigo in existing_compatibility:
                continue
            db.session.add(ScmReglaCompatibilidadReproceso(
                codigo=codigo,
                revision=1,
                nombre=nombre,
                estado="APROBADA",
                familia_objetivo_id=pp.id,
                proceso_objetivo_id=proceso_objetivo_id,
                familia_aporte_id=pp.id,
                proceso_aporte_id=proceso_aporte_id,
                resultado=resultado,
                porcentaje_maximo=porcentaje,
                simetrica=simetrica,
                notas="Semilla editable y versionada del piloto.",
                creado_por_id=seed_actor.id,
                aprobado_por_id=seed_actor.id,
                approved_at=db.func.now(),
            ))

        for code, name, threshold, unit, severity in (
            (
                "PESAJE_TARDIO_PREETIQUETA",
                "Pesaje tardio desde preetiqueta",
                24,
                "HORAS",
                "ADVERTENCIA",
            ),
            (
                "CORRECCION_PESAJE_TARDIA",
                "Correccion o anulacion tardia de pesaje",
                24,
                "HORAS",
                "ADVERTENCIA",
            ),
            (
                "PESAJE_FECHA_OPERATIVA_DIFERENTE",
                "Pesaje posterior a la fecha operativa",
                1,
                "DIAS_CALENDARIO",
                "ADVERTENCIA",
            ),
            (
                "DIFERENCIA_CUSTODIA_MERMA",
                "Diferencia entre almacen y pre-molino",
                1,
                "KG",
                "CRITICA",
            ),
            (
                "TRANSFERENCIA_DIFERENCIA",
                "Diferencia al recibir una transferencia",
                1,
                "KG",
                "CRITICA",
            ),
            (
                "MANGA_PESADA_SIN_RECEPCION",
                "Manga pesada sin recepcion de almacen",
                24,
                "HORAS",
                "ADVERTENCIA",
            ),
        ):
            seed_alert_rule(
                db.session,
                code=code,
                name=name,
                threshold=threshold,
                unit=unit,
                severity=severity,
                actor_id=seed_actor.id,
            )

    db.session.commit()
    return ScmSeedResult(
        capacidades_creadas=capacidades_creadas,
        roles_creados=roles_creados,
        categorias_creadas=categorias_creadas,
        relaciones_creadas=relaciones_creadas,
    )
