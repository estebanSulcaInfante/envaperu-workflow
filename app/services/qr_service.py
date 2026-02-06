"""
Servicio de generación de códigos QR para Órdenes de Producción.
Genera QR que redirige a Google Forms con datos pre-llenados.
"""
import qrcode
from io import BytesIO
from urllib.parse import urlencode, quote_plus

# URL base del Google Form
GOOGLE_FORM_BASE_URL = "https://docs.google.com/forms/d/e/1FAIpQLSdf85x8DVmY_szgqvlwBgOWcdJWLqJZI5IafMdygaNk-QcA8g/viewform"

# IDs de los campos del formulario
FORM_FIELDS = {
    'numero_op': 'entry.374896580',
    'molde': 'entry.1779940712',
    'peso_unitario': 'entry.885430358',
    'maquina': 'entry.873760233',
}


def generar_url_form(orden) -> str:
    """
    Genera la URL del Google Form con los datos de la orden pre-llenados.
    
    Args:
        orden: Objeto OrdenProduccion
        
    Returns:
        str: URL completa con parámetros
    """
    params = {
        'usp': 'pp_url',
        FORM_FIELDS['numero_op']: orden.numero_op or '',
        FORM_FIELDS['molde']: orden.molde or '',
        FORM_FIELDS['peso_unitario']: str(int(orden.snapshot_peso_unitario_gr)) if orden.snapshot_peso_unitario_gr else '',
        FORM_FIELDS['maquina']: orden.maquina_id or '',
    }
    
    # Construir URL con encoding correcto para espacios (+)
    query_string = urlencode(params, quote_via=quote_plus)
    return f"{GOOGLE_FORM_BASE_URL}?{query_string}"


def generar_qr_imagen(orden, size: int = 200) -> BytesIO:
    """
    Genera una imagen QR con la URL del Google Form.
    
    Args:
        orden: Objeto OrdenProduccion
        size: Tamaño en píxeles del QR (ancho = alto)
        
    Returns:
        BytesIO: Buffer con la imagen PNG del QR
    """
    url = generar_url_form(orden)
    
    # Crear QR
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    
    # Generar imagen
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Redimensionar si es necesario
    if size != img.size[0]:
        img = img.resize((size, size))
    
    # Guardar a buffer
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    return buffer


def generar_qr_base64(orden, size: int = 200) -> str:
    """
    Genera el QR como string base64 (útil para frontend).
    
    Args:
        orden: Objeto OrdenProduccion
        size: Tamaño en píxeles
        
    Returns:
        str: Imagen en formato data URI (base64)
    """
    import base64
    
    buffer = generar_qr_imagen(orden, size)
    b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    
    return f"data:image/png;base64,{b64}"
