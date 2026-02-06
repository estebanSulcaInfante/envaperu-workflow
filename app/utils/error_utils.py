"""
Utilidades centralizadas para manejo de errores y respuestas estandarizadas.
Provee funciones helper para respuestas de error consistentes y logging estructurado.
"""
from flask import jsonify, current_app
from functools import wraps
import traceback
import logging
from datetime import datetime

# Configurar logger
logger = logging.getLogger('envaperu')


class APIError(Exception):
    """
    Excepción personalizada para errores de API.
    Permite especificar código HTTP y mensaje.
    """
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['error'] = self.message
        rv['status'] = self.status_code
        rv['timestamp'] = datetime.utcnow().isoformat()
        return rv


# Errores predefinidos con mensajes amigables
class ErrorCodes:
    """Códigos de error estandarizados"""
    VALIDATION_ERROR = ('VALIDATION_ERROR', 400)
    NOT_FOUND = ('NOT_FOUND', 404)
    DUPLICATE = ('DUPLICATE', 409)
    SERVER_ERROR = ('SERVER_ERROR', 500)
    UNAUTHORIZED = ('UNAUTHORIZED', 401)
    FORBIDDEN = ('FORBIDDEN', 403)


def error_response(message, status_code=400, code=None, details=None):
    """
    Genera una respuesta de error estandarizada.
    
    Args:
        message: Mensaje de error para el usuario
        status_code: Código HTTP (default 400)
        code: Código de error interno (opcional)
        details: Detalles adicionales (opcional, solo en desarrollo)
    
    Returns:
        tuple: (response, status_code)
    """
    response = {
        'error': message,
        'status': status_code,
        'timestamp': datetime.utcnow().isoformat()
    }
    
    if code:
        response['code'] = code
    
    # Solo incluir detalles técnicos en desarrollo
    if details and current_app.debug:
        response['details'] = details
    
    return jsonify(response), status_code


def success_response(data=None, message=None, status_code=200):
    """
    Genera una respuesta de éxito estandarizada.
    
    Args:
        data: Datos a retornar
        message: Mensaje opcional
        status_code: Código HTTP (default 200)
    
    Returns:
        tuple: (response, status_code)
    """
    response = {'success': True}
    
    if data is not None:
        response['data'] = data
    
    if message:
        response['message'] = message
    
    return jsonify(response), status_code


def handle_errors(f):
    """
    Decorator para manejar errores en rutas de Flask.
    Captura excepciones y las convierte en respuestas JSON estandarizadas.
    
    Uso:
        @app.route('/api/example')
        @handle_errors
        def example_route():
            ...
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except APIError as e:
            logger.warning(f"APIError in {f.__name__}: {e.message}")
            return jsonify(e.to_dict()), e.status_code
        except ValueError as e:
            logger.warning(f"ValueError in {f.__name__}: {str(e)}")
            return error_response(str(e), 400, 'VALIDATION_ERROR')
        except KeyError as e:
            logger.warning(f"KeyError in {f.__name__}: Missing key {e}")
            return error_response(f"Campo requerido faltante: {e}", 400, 'MISSING_FIELD')
        except Exception as e:
            # Log completo del error para debugging
            logger.error(f"Unhandled error in {f.__name__}: {str(e)}")
            logger.error(traceback.format_exc())
            
            # Respuesta genérica al usuario
            return error_response(
                "Error interno del servidor. Por favor, intente más tarde.",
                500,
                'SERVER_ERROR',
                details=str(e) if current_app.debug else None
            )
    return decorated_function


def log_request(route_name, **context):
    """
    Registra información de una petición con contexto.
    
    Args:
        route_name: Nombre de la ruta/operación
        **context: Datos adicionales de contexto (user, orden_id, etc.)
    """
    log_data = {
        'route': route_name,
        'timestamp': datetime.utcnow().isoformat(),
        **context
    }
    logger.info(f"REQUEST: {log_data}")


def log_operation(operation, status='success', **context):
    """
    Registra el resultado de una operación.
    
    Args:
        operation: Nombre de la operación (crear_orden, actualizar_registro, etc.)
        status: 'success', 'warning', 'error'
        **context: Datos adicionales
    """
    log_data = {
        'operation': operation,
        'status': status,
        'timestamp': datetime.utcnow().isoformat(),
        **context
    }
    
    if status == 'error':
        logger.error(f"OPERATION: {log_data}")
    elif status == 'warning':
        logger.warning(f"OPERATION: {log_data}")
    else:
        logger.info(f"OPERATION: {log_data}")


# Helper para validación
def validate_required(data, required_fields):
    """
    Valida que todos los campos requeridos estén presentes.
    
    Args:
        data: Dict con los datos a validar
        required_fields: Lista de campos requeridos
    
    Raises:
        APIError: Si falta algún campo
    """
    missing = [f for f in required_fields if f not in data or data[f] is None]
    if missing:
        raise APIError(
            f"Campos requeridos faltantes: {', '.join(missing)}",
            400
        )
