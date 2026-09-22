"""Excel export for the PT availability projection.

The workbook deliberately keeps a normalized component sheet and adds a
wide matrix sheet for the day-to-day spreadsheet work requested by the PT
warehouse owner.  Both are projections of ``list_pt_availability``; they do
not introduce a second stock calculation.
"""

from datetime import datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

from app.services.scm_kg_pt_availability_service import list_pt_availability
from app.services.scm_service_support import ScmServiceError


XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_MATRIX_COMPONENTS = 100
MAX_EXPORT_CELLS = 250_000


def _safe_text(value):
    """Prevent spreadsheet formula injection while preserving displayed text."""
    if value is None:
        return None
    text = str(value)
    return f"'{text}" if text[:1] in {"=", "+", "-", "@"} else text


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _piece_fields(component):
    identity = component.get("identidad_pieza") or {}
    article = component.get("articulo") or {}
    # WIP intentionally has no fabricated colour or piece identity.
    return (
        identity.get("nombre") or article.get("nombre"),
        identity.get("color_nombre"),
        article.get("codigo"),
    )


def _status(component):
    if component.get("estado") != "CALCULABLE":
        return "Sin referencia de peso"
    if component.get("es_limitante"):
        return "Limitante"
    if _number(component.get("faltante_kg")) and _number(component.get("faltante_kg")) > 0:
        return "Faltante"
    return "Disponible"


def _component_columns(component, prefix="Componente"):
    name, color, code = _piece_fields(component)
    return [
        name,
        color,
        code,
        "WIP" if component.get("naturaleza") == "SUBENSAMBLE_WIP" else "Pieza",
        _number(component.get("cantidad_bom_un")),
        _number(component.get("kg_disponibles")),
        _number(component.get("kg_requeridos_por_un_pt")),
        _number(component.get("faltante_kg")),
        _number(component.get("cobertura_un")),
        _status(component),
        "Sí" if component.get("es_limitante") else "No",
        component.get("grupo_stock_compartido"),
    ]


def _setup_sheet(sheet, headers, rows, table_name):
    sheet.append([_safe_text(header) for header in headers])
    for row in rows:
        sheet.append([_safe_text(value) if isinstance(value, str) else value for value in row])
    sheet.freeze_panes = "A2"
    last_column = get_column_letter(max(1, len(headers)))
    sheet.auto_filter.ref = f"A1:{last_column}{max(1, sheet.max_row)}"
    if rows:
        table = Table(displayName=table_name, ref=f"A1:{last_column}{sheet.max_row}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2", showFirstColumn=False,
            showLastColumn=False, showRowStripes=True, showColumnStripes=False,
        )
        sheet.add_table(table)
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for column_cells in sheet.columns:
        width = min(42, max(12, max(len(str(cell.value or "")) for cell in column_cells) + 2))
        sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = width
    for column_index, header in enumerate(headers, start=1):
        if not any(token in header for token in (" KG", " UN", "KG/PT", "Cobertura")):
            continue
        for row_index in range(2, sheet.max_row + 1):
            cell = sheet.cell(row=row_index, column=column_index)
            if isinstance(cell.value, (int, float)):
                cell.number_format = "0.000"


def generate_pt_availability_xlsx(session, *, actor_id, query=None, location=None):
    """Return an in-memory workbook for the filtered PT availability view."""
    generated_at = datetime.now(timezone.utc)
    generated_at_text = generated_at.isoformat()
    payload = list_pt_availability(session, actor_id=actor_id, query=query, location=location)
    items = payload.get("items") or []
    component_count = sum(len(item.get("componentes") or []) for item in items)
    max_components = max((len(item.get("componentes") or []) for item in items), default=0)
    estimated_cells = (len(items) * (18 + 6 + 12 * max_components)) + (component_count * 23)
    if max_components > MAX_MATRIX_COMPONENTS or estimated_cells > MAX_EXPORT_CELLS:
        raise ScmServiceError(
            "PT_EXPORT_TOO_LARGE",
            "La consulta es demasiado grande para la exportación Excel interactiva. Aplica un filtro y vuelve a intentarlo.",
            status_code=413,
            details={"pt_count": len(items), "component_count": component_count},
        )

    summary_headers = [
        "PT código", "PT nombre", "BOM revisión", "Hash BOM", "Saldo PT UN",
        "Potencial estimado UN", "Estado potencial", "Motivo potencial",
        "Restricción nombre", "Restricción color", "Restricción código",
        "Restricción tipo", "Disponible restricción KG", "Requerido por PT KG",
        "Faltante KG", "Estado restricción", "Grupo stock compartido",
        "Potencial sumable", "Generado UTC",
    ]
    summary_rows = []
    component_rows = []
    matrix_inputs = []
    max_components = 0
    for item in items:
        product = item.get("pt") or {}
        components = item.get("componentes") or []
        max_components = max(max_components, len(components))
        limiting = next((c for c in components if c.get("es_limitante")), None)
        if limiting is None and item.get("potencial_estado") == "CALCULABLE":
            limiting = next((c for c in components if _number(c.get("faltante_kg")) and _number(c.get("faltante_kg")) > 0), None)
        if limiting is None and item.get("potencial_estado") != "CALCULABLE":
            limiting = next((c for c in components if c.get("estado") != "CALCULABLE"), None)
        lim_name, lim_color, lim_code = _piece_fields(limiting or {})
        summary_rows.append([
            product.get("codigo"), product.get("nombre"),
            (item.get("revision_bom") or {}).get("numero"),
            (item.get("revision_bom") or {}).get("content_hash"),
            _number(item.get("saldo_manual_un")),
            _number(item.get("potencial_un_estimado")), item.get("potencial_estado"),
            item.get("potencial_motivo"), lim_name, lim_color, lim_code,
            "WIP" if limiting and limiting.get("naturaleza") == "SUBENSAMBLE_WIP" else ("Pieza" if limiting else None),
            _number(limiting.get("kg_disponibles")) if limiting else None,
            _number(limiting.get("kg_requeridos_por_un_pt")) if limiting else None,
            _number(limiting.get("faltante_kg")) if limiting else None,
            _status(limiting) if limiting else None,
            limiting.get("grupo_stock_compartido") if limiting else None,
            "Sí" if item.get("potencial_sumable") else "No", generated_at_text,
        ])
        matrix_inputs.append((item, components))
        for sequence, component in enumerate(components, start=1):
            name, color, code = _piece_fields(component)
            component_rows.append([
                product.get("codigo"), product.get("nombre"),
                (item.get("revision_bom") or {}).get("numero"),
                (item.get("revision_bom") or {}).get("content_hash"),
                item.get("potencial_estado"), item.get("potencial_motivo"),
                _number(item.get("potencial_un_estimado")), sequence,
                name, color, code,
                "WIP" if component.get("naturaleza") == "SUBENSAMBLE_WIP" else "Pieza",
                _number(component.get("cantidad_bom_un")),
                _number(component.get("peso_unitario_kg")),
                _number(component.get("kg_disponibles")),
                _number(component.get("kg_requeridos_por_un_pt")),
                _number(component.get("faltante_kg")), _number(component.get("cobertura_un")),
                _status(component), "Sí" if component.get("es_limitante") else "No",
                component.get("grupo_stock_compartido"),
                "Sí" if component.get("potencial_sumable") else "No",
                generated_at_text, location or "(todas las ubicaciones autorizadas)",
            ])

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Resumen PT"
    _setup_sheet(summary, summary_headers, summary_rows, "ResumenPT")

    components = workbook.create_sheet("Componentes BOM")
    _setup_sheet(components, [
        "PT código", "PT nombre", "BOM revisión", "Hash BOM",
        "Estado potencial", "Motivo potencial", "Potencial estimado UN", "Secuencia",
        "Pieza nombre", "Color", "Componente código", "Tipo",
        "Cantidad BOM UN", "Peso unitario KG", "Disponible KG", "Requerido por 1 PT KG",
        "Faltante KG", "Cobertura UN", "Estado", "Limitante", "Grupo stock compartido",
        "Potencial sumable", "Generado UTC", "Filtro ubicación",
    ], component_rows, "ComponentesBOM")

    matrix = workbook.create_sheet("Matriz BOM")
    matrix_headers = ["PT código", "PT nombre", "BOM revisión", "Saldo PT UN", "Potencial estimado UN", "Estado potencial"]
    for sequence in range(1, max_components + 1):
        matrix_headers.extend([
            f"Pieza {sequence} nombre", f"Pieza {sequence} color", f"Pieza {sequence} código",
            f"Pieza {sequence} tipo", f"Pieza {sequence} cantidad BOM UN",
            f"Pieza {sequence} disponible KG", f"Pieza {sequence} requerido KG/PT",
            f"Pieza {sequence} faltante KG", f"Pieza {sequence} cobertura UN",
            f"Pieza {sequence} estado", f"Pieza {sequence} limitante", f"Pieza {sequence} grupo stock",
        ])
    matrix_rows = []
    for item, item_components in matrix_inputs:
        product = item.get("pt") or {}
        row = [
            product.get("codigo"), product.get("nombre"),
            (item.get("revision_bom") or {}).get("numero"),
            _number(item.get("saldo_manual_un")), _number(item.get("potencial_un_estimado")),
            item.get("potencial_estado"),
        ]
        for component in item_components:
            row.extend(_component_columns(component))
        row.extend([None] * (12 * (max_components - len(item_components))))
        matrix_rows.append(row)
    _setup_sheet(matrix, matrix_headers, matrix_rows, "MatrizBOM")

    info = workbook.create_sheet("Información")
    info_rows = [
        ["Formato", "DISPONIBILIDAD_PT_XLSX_V1"],
        ["Generado en UTC", generated_at_text],
        ["Filtro búsqueda aplicado", query or "(todos)"],
        ["Filtro ubicación aplicado", location or "(todas las ubicaciones autorizadas)"],
        ["Filas PT exportadas", len(items)],
        ["Fuente cálculo", "Disponibilidad KG medida desde pesaje y Kardex PT manual"],
        ["Política piloto", payload.get("politica_piloto")],
        ["Observación", "Los potenciales PT alternativos comparten stock y no se suman."],
        ["Vigencia", "La hora indica cuándo se generó la consulta; no representa un snapshot transaccional único."],
    ]
    _setup_sheet(info, ["Campo", "Valor"], info_rows, "Informacion")

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
