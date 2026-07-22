# Importar todos los modelos para facilitar acceso
from app.models.materiales import MateriaPrima, Colorante
from app.models.correlativo_catalogo import CorrelativoCatalogo
from app.models.scm_catalogos import (
    ScmCapacidad,
    ScmCategoriaRecepcion,
    ScmMaterial,
    ScmProveedor,
)
from app.models.scm_compras import (
    ScmOrdenCompra,
    ScmOrdenCompraLinea,
    ScmOrdenCompraRevision,
)
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_recepcion import (
    ScmDocumentoProveedor,
    ScmPesajeBolsa,
    ScmRecepcion,
    ScmRecepcionDocumento,
    ScmRecepcionLinea,
)
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor, LoteSalidaPiezaColor
from app.models.recetas import SeCompone, SeColorea
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    Familia,
    FamiliaColor,
    Linea,
    LineaFamilia,
    PiezaColor,
    PiezaComponente,
    ProductoPieza,
    ProductoTerminado,
)
from app.models.maquina import Maquina, TipoMaquina
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.control_peso import ControlPeso
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.talonario import Talonario
from app.models.historial_estado import HistorialEstadoOrden
from app.models.receta_color import (
    RecetaColorLinea,
    RecetaColorMaestra,
    RecetaColorNormalizada,
)
from app.models.trabajador import Trabajador, RolOperativo
from app.models.estacion_pesaje import (
    EstacionPesaje,
    EstacionEstadoActual,
    EstacionHeartbeatRecepcion,
    EstacionEstadoHistorial,
    EstacionReporteAvanceRecepcion,
    EstacionAvanceProduccion,
)
from app.models.legacy_pesaje import (
    EstacionComandoPiloto,
    EstacionCierreOpLegacy,
    EstacionDeltaPesajeLegacy,
    EstacionImportacionPesajeLegacy,
    EstacionImportacionPesajeLegacyChunk,
    EstacionImportacionPesajeLegacyFila,
    EstacionPesajeLegacy,
)
