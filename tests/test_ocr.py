"""
Simple OCR Test Script
"""
import os
import sys

# Load env
from dotenv import load_dotenv
load_dotenv()

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app.services.ocr_service import extract_data_from_image
except ImportError:
    import pytest
    pytest.skip("skipping OCR tests because google module is missing", allow_module_level=True)
import json

def test_ocr():
    image_path = "app/templates/OCR/RegistroDiarioProduccion/image.png"
    
    if not os.path.exists(image_path):
        print(f"ERROR: Image not found at {image_path}")
        return
    
    print(f"Reading image: {image_path}")
    with open(image_path, 'rb') as f:
        image_bytes = f.read()
    
    print(f"Image size: {len(image_bytes)} bytes")
    print("Sending to Gemini Vision API...")
    
    result = extract_data_from_image(image_bytes)
    
    print("\n" + "="*50)
    print("RESULT:")
    print("="*50)
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    test_ocr()
