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
from app.models.scm_articulos import (
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
    ScmDefinicionWip,
)
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionPrecedencia,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_commercial import ScmPresentacionComercial
from app.models.scm_ot import (
    ScmAsignacionPlanMangaOt,
    ScmEtiquetaManga,
    ScmLoteArticulo,
    ScmManga,
    ScmPlanMangaOp,
    ScmPlanMangaOpLinea,
    ScmSolicitudMangaExtra,
    ScmTrabajoImpresionManga,
    ScmPesajeManga,
    ScmAnulacionPesajeManga,
    ScmCorreccionPesajeManga,
)
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    ScmPlanProduccion,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
)
from app.models.scm_inventory import (
    ScmLoteAperturaInventario,
    ScmLoteAperturaLinea,
    ScmMovimientoMaterialInventario,
    ScmMovimientoInventario,
    ScmReservaInventario,
    ScmSaldoInventario,
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.models.scm_warehouse import (
    ScmExistenciaManga,
    ScmRechazoRecepcionManga,
    ScmReversionRecepcionManga,
    ScmSesionRecepcionManga,
)
from app.models.scm_internal_supply import (
    ScmAsignacionAbastecimiento,
    ScmAsignacionPoolArmado,
    ScmPoolOrigenArmado,
    ScmSolicitudAbastecimiento,
    ScmSolicitudAbastecimientoLinea,
)
from app.models.scm_assembly_execution import (
    ScmConfirmacionMangaArmado,
    ScmConsumoComponenteArmado,
    ScmCorreccionMangaArmado,
)
from app.models.scm_material_execution import (
    ScmDevolucionMaterial,
    ScmEmisionMaterial,
    ScmLotePremezcla,
    ScmLotePremezclaInput,
    ScmRequerimientoMaterial,
    ScmReservaMaterial,
)
from app.models.scm_reproceso import (
    ScmAlertaEvento,
    ScmAlertaOperativa,
    ScmCondicionMerma,
    ScmFamiliaMaterialReproceso,
    ScmLoteMaterialRecuperado,
    ScmLoteMermaRecuperable,
    ScmMovimientoMerma,
    ScmOrdenMolienda,
    ScmOrdenMoliendaAporte,
    ScmProcesoMaterialReproceso,
    ScmReglaAlerta,
    ScmReglaAlertaRevision,
    ScmReglaCompatibilidadReproceso,
)
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
