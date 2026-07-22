from uuid import uuid4

from sqlalchemy import select

from app.models.materiales import Colorante, MateriaPrima
from app.models.scm_catalogos import (
    CLASE_COLORANTE,
    CLASE_MATERIA_PRIMA,
    ScmCategoriaRecepcion,
    ScmMaterial,
)


class ScmMaterialConfigurationError(RuntimeError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _normalized_name(nombre):
    normalized = str(nombre or "").strip()
    if not normalized:
        raise ScmMaterialConfigurationError(
            "SCM_MATERIAL_NAME_REQUIRED",
            "El material requiere un nombre.",
        )
    return normalized


def _normalized_materia_prima_type(tipo):
    normalized = str(tipo or "").strip().upper()
    return normalized or None


def _category_code_for_materia_prima(tipo):
    if tipo == "VIRGEN":
        return "RESINA_VIRGEN"
    if tipo == "SEGUNDA":
        return "RESINA_SEGUNDA"
    return "LEGACY_POR_CONFIGURAR"


def _required_category(session, codigo):
    categoria = session.scalar(
        select(ScmCategoriaRecepcion).where(
            ScmCategoriaRecepcion.codigo == codigo
        )
    )
    if categoria is None:
        raise ScmMaterialConfigurationError(
            "SCM_CATEGORY_NOT_CONFIGURED",
            f"La categoría técnica {codigo} no está configurada.",
            details={"categoria_codigo": codigo},
        )
    if not categoria.activo:
        raise ScmMaterialConfigurationError(
            "SCM_CATEGORY_INACTIVE",
            f"La categoría técnica {codigo} está inactiva.",
            details={"categoria_codigo": codigo},
        )
    return categoria


def _technical_code(prefix):
    return f"{prefix}-AUTO-{uuid4().hex.upper()}"


def _normalized_scm_code(codigo_scm, prefix):
    if codigo_scm is None:
        return _technical_code(prefix)
    normalized = str(codigo_scm).strip().upper()
    if not normalized:
        raise ScmMaterialConfigurationError(
            "SCM_MATERIAL_CODE_REQUIRED",
            "El material requiere un código SCM.",
        )
    return normalized


def ensure_materia_prima_identity(
    *,
    session,
    materia_prima,
    categoria_codigo=None,
    codigo_scm=None,
):
    if materia_prima.scm_material is not None:
        return materia_prima.scm_material

    normalized_type = _normalized_materia_prima_type(materia_prima.tipo)
    materia_prima.nombre = _normalized_name(materia_prima.nombre)
    materia_prima.tipo = normalized_type
    categoria = _required_category(
        session,
        categoria_codigo or _category_code_for_materia_prima(normalized_type),
    )
    material = ScmMaterial(
        codigo=_normalized_scm_code(codigo_scm, "MP"),
        nombre=materia_prima.nombre,
        clase=CLASE_MATERIA_PRIMA,
        categoria_recepcion=categoria,
    )
    materia_prima.scm_material = material
    session.add(materia_prima)
    return material


def ensure_colorante_identity(
    *,
    session,
    colorante,
    categoria_codigo="LEGACY_POR_CONFIGURAR",
    codigo_scm=None,
):
    if colorante.scm_material is not None:
        return colorante.scm_material

    colorante.nombre = _normalized_name(colorante.nombre)
    categoria = _required_category(session, categoria_codigo)
    material = ScmMaterial(
        codigo=_normalized_scm_code(codigo_scm, "COL"),
        nombre=colorante.nombre,
        clase=CLASE_COLORANTE,
        categoria_recepcion=categoria,
    )
    colorante.scm_material = material
    session.add(colorante)
    return material


def create_materia_prima_with_scm(
    *,
    session,
    nombre,
    tipo=None,
    categoria_codigo=None,
    codigo_scm=None,
):
    """Agrega ambos registros a la sesión; el llamador controla commit/rollback."""
    materia_prima = MateriaPrima(
        nombre=nombre,
        tipo=tipo,
    )
    ensure_materia_prima_identity(
        session=session,
        materia_prima=materia_prima,
        categoria_codigo=categoria_codigo,
        codigo_scm=codigo_scm,
    )
    return materia_prima


def create_colorante_with_scm(
    *,
    session,
    nombre,
    categoria_codigo="LEGACY_POR_CONFIGURAR",
    codigo_scm=None,
):
    """Agrega el colorante y su identidad común; el llamador hace commit."""
    colorante = Colorante(nombre=nombre)
    ensure_colorante_identity(
        session=session,
        colorante=colorante,
        categoria_codigo=categoria_codigo,
        codigo_scm=codigo_scm,
    )
    return colorante
