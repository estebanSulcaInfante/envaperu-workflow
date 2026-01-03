"""
OCR Service using Gemini Vision API (New google.genai library)
Extracts structured data from production report photos
"""
from google import genai
from google.genai import types
import json
import base64
import os
from PIL import Image
import io

EXTRACTION_PROMPT = """
Analiza esta imagen de un "REGISTRO DIARIO DE PRODUCCIÓN" (formulario de planta) y extrae todos los datos escritos a mano.

Devuelve ÚNICAMENTE un JSON válido con la siguiente estructura (sin explicaciones adicionales):
REGLAS:
1. Si no se puede encontrar un campo ponerle Null, excepto por color y maquinista, estos completalos con el primer color visto hasta un cambio de color.
2. Los números deben ser enteros sin comillas
3. El array "detalles" debe tener una entrada por cada fila horaria que tenga datos
4. Si hay dos colores separados por "/" indica cambio de color (mantener como string)
5. Responde SOLO con el JSON, sin markdown ni explicaciones
6. las comillas "" en maquinista y color representan valores repetidos de la fila anterior
7. Para las horas, hay exactamente 11 filas.
{
  "fecha": "YYYY-MM-DD o null si no se lee",
  "turno": "DIURNO" o "NOCTURNO" o "EXTRA" o null,
  "hora_inicio": "HH:MM o null",
  "colada_inicial": número o null,
  "colada_final": número o null,
  "enfriamiento": número o null,
  "cantidad_por_hora": número o null,
  "detalles": [
    {
      "hora": "HH:MM - HH:MM",
      "maquinista": "nombre o null",
      "color": "AMARILLO,ANARANJADO, AZUL, BLANCO, CARNE, CELESTE, CREMA, FUCSIA, LILA,LILA BEBE, MARRON, MELON, NEGRO, PLOMO, ROJO, ROSADO, TURQUESA, VERDE o null", 
      "coladas": INT o 0,
      "observacion": "texto o null"
    }
  ]
}
"""


def extract_data_from_image(image_bytes: bytes, api_key=None) -> dict:
    """
    Extract production report data from an image using Gemini Vision.
    
    Args:
        image_bytes: Raw image bytes (JPEG/PNG)
        api_key: Optional API key override
        
    Returns:
        dict with extracted data and confidence info
    """
    key = api_key or os.environ.get('GEMINI_API_KEY')
    if not key:
        return {"success": False, "error": "GEMINI_API_KEY not configured"}
    
    # Create client
    client = genai.Client(api_key=key)
    
    # Prepare image
    image = Image.open(io.BytesIO(image_bytes))
    
    # Convert to bytes for the API
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='PNG')
    img_byte_arr = img_byte_arr.getvalue()
    
    try:
        # Send to Gemini 
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[
                EXTRACTION_PROMPT,
                types.Part.from_bytes(data=img_byte_arr, mime_type='image/png')
            ]
        )
        
        # Parse response
        response_text = response.text.strip()
        
        # Clean up response (remove markdown code blocks if present)
        if response_text.startswith('```'):
            lines = response_text.split('\n')
            # Remove first and last line (```json and ```)
            response_text = '\n'.join(lines[1:-1])
        
        # Parse JSON
        data = json.loads(response_text)
        
        return {
            "success": True,
            "data": data,
            "raw_response": response_text
        }
        
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Error parsing JSON response: {str(e)}",
            "raw_response": response_text if 'response_text' in locals() else None
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def extract_from_base64(base64_string: str, api_key=None) -> dict:
    """
    Extract data from a base64-encoded image.
    """
    # Remove data URL prefix if present
    if ',' in base64_string:
        base64_string = base64_string.split(',')[1]
    
    image_bytes = base64.b64decode(base64_string)
    return extract_data_from_image(image_bytes, api_key)
