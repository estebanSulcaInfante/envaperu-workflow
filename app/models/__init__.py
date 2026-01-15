# Importar todos los modelos para facilitar acceso
from app.models.materiales import MateriaPrima, Colorante
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.producto import ProductoTerminado, Pieza, ProductoPieza, FamiliaColor, ColorProducto, PiezaComponente
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.control_peso import ControlPeso
from app.models.molde import Molde, MoldePieza
from app.models.talonario import Talonario
from app.models.historial_estado import HistorialEstadoOrden
