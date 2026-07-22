from dataclasses import dataclass

from app.extensions import db
from app.models.scm_catalogos import (
    MODALIDAD_POR_CONFIGURAR,
    MODALIDAD_SEGUNDA,
    MODALIDAD_VIRGEN,
    ScmCapacidad,
    ScmCategoriaRecepcion,
)
from app.models.trabajador import RolOperativo


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
)


ROLES_SCM_INICIALES = (
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
        ),
    ),
    ("CALIDAD", "Calidad", ("CALIDAD_RESOLVER",)),
    (
        "GERENCIA",
        "Gerencia",
        ("OC_APROBAR", "CORRECCION_APROBAR"),
    ),
    (
        "SUPERVISOR",
        "Supervisor",
        ("ENTRADA_EXCEPCIONAL_REGULARIZAR",),
    ),
    (
        "CONFIGURACION_SCM",
        "Configuración SCM",
        (
            "CONFIG_RECEPCION_ADMINISTRAR",
            "LIBERACION_DIRECTA_ADMINISTRAR",
        ),
    ),
    ("AUDITORIA_CONSULTA", "Auditoría / Consulta", ()),
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
        if rol is not None:
            continue

        rol = RolOperativo(codigo=codigo, nombre=nombre, activo=True)
        rol.capacidades = [capacidades[item] for item in codigos_capacidad]
        db.session.add(rol)
        roles[codigo] = rol
        roles_creados += 1
        relaciones_creadas += len(codigos_capacidad)

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

    db.session.commit()
    return ScmSeedResult(
        capacidades_creadas=capacidades_creadas,
        roles_creados=roles_creados,
        categorias_creadas=categorias_creadas,
        relaciones_creadas=relaciones_creadas,
    )
