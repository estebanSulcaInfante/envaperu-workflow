"""
Servicio de generación de Excel para Órdenes de Producción.
Llena la plantilla "IMPRIMIR OP" con los datos de una orden.
"""
import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils import get_column_letter
from io import BytesIO
from datetime import datetime
import os

# Ruta a la plantilla Excel
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), '..', 'templates', 'excel', 'OrdenProduccion', 'Book1.xlsx')


def generar_op_excel(orden) -> BytesIO:
    """
    Genera un archivo Excel llenando la pestaña "IMPRIMIR OP" con los datos de la orden.
    
    Args:
        orden: Objeto OrdenProduccion con sus lotes cargados
        
    Returns:
        BytesIO: Buffer con el archivo Excel listo para descarga
    """
    # Cargar plantilla
    wb = openpyxl.load_workbook(TEMPLATE_PATH)
    ws = wb['IMPRIMIR OP']
    
    # Obtener datos calculados
    resumen = orden.resumen_totales
    
    # =========================================================================
    # 1. CABECERA (Filas 1-12)
    # =========================================================================
    ws['D1'] = orden.numero_op
    ws['C3'] = orden.fecha_creacion.strftime('%Y-%m-%d') if orden.fecha_creacion else ''
    ws['C4'] = orden.producto or ''
    ws['G4'] = orden.maquina_id or ''
    ws['C5'] = resumen.get('Cantidad DOC', 0)
    ws['B7'] = orden.molde or ''
    ws['C8'] = resumen.get('Días', 0)
    ws['G8'] = orden.snapshot_peso_unitario_gr or 0
    ws['C9'] = orden.snapshot_horas_turno or 24
    ws['G9'] = orden.snapshot_peso_inc_colada or 0
    ws['C10'] = orden.snapshot_cavidades or 1
    ws['G10'] = resumen.get('%Merma', 0)
    
    # Coladas por hora: 3600 / tiempo_ciclo
    coladas_hora = 0
    if orden.snapshot_tiempo_ciclo and orden.snapshot_tiempo_ciclo > 0:
        coladas_hora = 3600 / orden.snapshot_tiempo_ciclo
    ws['C11'] = coladas_hora
    
    # Rango de fechas
    fecha_inicio = orden.fecha_inicio.strftime('%d/%m') if orden.fecha_inicio else ''
    fecha_fin_str = resumen.get('F. Fin', '')
    if fecha_fin_str:
        try:
            fecha_fin = datetime.fromisoformat(fecha_fin_str)
            ws['G11'] = f"{fecha_inicio} a {fecha_fin.strftime('%d/%m')}"
        except:
            ws['G11'] = fecha_inicio
    else:
        ws['G11'] = fecha_inicio
    
    # =========================================================================
    # 2. TABLA DE COLORES / PRODUCCIÓN (Filas 14-21)
    # =========================================================================
    lotes = orden.lotes[:6]  # Máximo 6 colores
    
    # Totales para sumar
    total_peso = 0
    total_merma = 0
    total_coladas = 0
    
    for i, lote in enumerate(lotes):
        row = 15 + i  # Filas 15-20
        lote_data = lote.to_dict()
        
        # Nombre del color
        ws[f'B{row}'] = lote.color_rel.nombre if lote.color_rel else 'Sin Color'
        
        # Peso producción por color (peso base + extra)
        peso_lote = lote_data.get('TOTAL + EXTRA (Kg)', 0)
        ws[f'C{row}'] = peso_lote
        total_peso += peso_lote
        
        # Merma a recuperar (calculada)
        merma_pct = resumen.get('%Merma', 0)
        merma_lote = peso_lote * merma_pct if merma_pct else 0
        ws[f'E{row}'] = merma_lote
        total_merma += merma_lote
        
        # Cantidad coladas
        coladas = lote_data.get('coladas_calculadas', 0)
        ws[f'G{row}'] = coladas
        total_coladas += coladas
    
    # Fila de totales (21)
    ws['C21'] = total_peso
    ws['E21'] = total_merma
    ws['G21'] = total_coladas
    
    # =========================================================================
    # 3. MATERIA PRIMA TOTALES (Filas 23-28)
    # =========================================================================
    # Acumular materiales por tipo
    materiales_totales = {'VIRGEN': 0, 'VIRGEN_2': 0, 'MOLIDO': 0}
    materiales_nombres = {'VIRGEN': '', 'VIRGEN_2': '', 'MOLIDO': ''}
    
    for lote in lotes:
        for mat in lote.materias_primas:
            tipo = mat.materia.tipo if mat.materia else 'VIRGEN'
            peso = mat.peso_kg
            materiales_totales[tipo] = materiales_totales.get(tipo, 0) + peso
            if not materiales_nombres.get(tipo) and mat.materia:
                materiales_nombres[tipo] = mat.materia.nombre
    
    ws['D25'] = materiales_nombres.get('VIRGEN', '')
    ws['F25'] = materiales_totales.get('VIRGEN', 0)
    ws['D26'] = materiales_nombres.get('VIRGEN_2', '')
    ws['F26'] = materiales_totales.get('VIRGEN_2', 0)
    ws['D27'] = materiales_nombres.get('MOLIDO', '')
    ws['F27'] = materiales_totales.get('MOLIDO', 0)
    
    total_material = sum(materiales_totales.values())
    ws['F28'] = total_material
    
    # =========================================================================
    # 4. MATERIA PRIMA POR COLOR (Filas 30-38)
    # =========================================================================
    for i, lote in enumerate(lotes):
        row = 32 + i  # Filas 32-37
        
        ws[f'B{row}'] = lote.color_rel.nombre if lote.color_rel else 'Sin Color'
        
        # Procesar materiales del lote
        materiales_lote = {'VIRGEN': None, 'VIRGEN_2': None, 'MOLIDO': None}
        for mat in lote.materias_primas:
            tipo = mat.materia.tipo if mat.materia else 'VIRGEN'
            nombre = mat.materia.nombre if mat.materia else ''
            fraccion = mat.fraccion
            peso = mat.peso_kg
            materiales_lote[tipo] = {
                'nombre': f"{nombre} = {int(fraccion*6)}/6" if fraccion else nombre,
                'peso': peso
            }
        
        # Virgen 1 (C, D)
        if materiales_lote['VIRGEN']:
            ws[f'C{row}'] = materiales_lote['VIRGEN']['nombre']
            ws[f'D{row}'] = materiales_lote['VIRGEN']['peso']
        
        # Virgen 2 (E, F)
        if materiales_lote['VIRGEN_2']:
            ws[f'E{row}'] = materiales_lote['VIRGEN_2']['nombre']
            ws[f'F{row}'] = materiales_lote['VIRGEN_2']['peso']
        else:
            ws[f'F{row}'] = 0
        
        # Segunda (G, H)
        if materiales_lote['MOLIDO']:
            ws[f'G{row}'] = materiales_lote['MOLIDO']['nombre']
            ws[f'H{row}'] = materiales_lote['MOLIDO']['peso']
    
    # Totales materiales por color (fila 38)
    ws['D38'] = materiales_totales.get('VIRGEN', 0)
    ws['F38'] = materiales_totales.get('VIRGEN_2', 0)
    ws['H38'] = materiales_totales.get('MOLIDO', 0)
    
    # =========================================================================
    # 5. COLORANTES (Filas 40-56)
    # =========================================================================
    # Grupo 1: Colores 0-2 (filas 41-48)
    if len(lotes) >= 1:
        _llenar_colorantes_grupo(ws, lotes[0:3], start_header_row=41, start_data_row=42)
    
    # Grupo 2: Colores 3-5 (filas 49-56)
    if len(lotes) >= 4:
        _llenar_colorantes_grupo(ws, lotes[3:6], start_header_row=49, start_data_row=50)
    
    # =========================================================================
    # 6. QR CODE (Celda C61 - bajado 8 casillas desde C53)
    # =========================================================================
    from app.services.qr_service import generar_qr_imagen
    
    try:
        qr_buffer = generar_qr_imagen(orden, size=240)  # QR más grande
        qr_img = XLImage(qr_buffer)
        qr_img.width = 200
        qr_img.height = 200
        ws.add_image(qr_img, 'C61')
    except Exception as e:
        # Si falla el QR, continuamos sin él
        print(f"Error generando QR: {e}")
    
    # =========================================================================
    # GUARDAR A BUFFER
    # =========================================================================
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    
    return output


def _llenar_colorantes_grupo(ws, lotes_grupo, start_header_row, start_data_row):
    """
    Llena un grupo de colorantes (3 colores) en el Excel.
    
    Args:
        ws: Hoja de Excel
        lotes_grupo: Lista de hasta 3 lotes
        start_header_row: Fila donde van los nombres de colores
        start_data_row: Fila donde empiezan los colorantes
    """
    # Columnas para cada color del grupo
    columnas = [('C', 'D'), ('E', 'F'), ('G', 'H')]
    
    for idx, lote in enumerate(lotes_grupo):
        if idx >= 3:
            break
        
        col_nombre, col_gramos = columnas[idx]
        
        # Header: Nombre del color
        ws[f'{col_nombre}{start_header_row}'] = lote.color_rel.nombre if lote.color_rel else 'Sin Color'
        ws[f'{col_gramos}{start_header_row}'] = 'Gr.'
        
        # Colorantes (hasta 7 filas)
        # Data: Colorantes
        for i, pig in enumerate(lote.colorantes):
            row = start_data_row + i
            if row > start_data_row + 20: # Limite seguridad
                break
                
            ws[f'{col_nombre}{row}'] = pig.pigmento.nombre if pig.pigmento else ''
            ws[f'{col_gramos}{row}'] = (pig.gramos or 0) * 2  # Multiplicador x2 por requerimiento de impresión

