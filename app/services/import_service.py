"""
Servicio de importación de catálogo desde Excel/CSV.
Maneja Productos Terminados y Piezas con normalización de colores.
Incluye validación detallada y manejo robusto de errores.
"""
import pandas as pd
from io import BytesIO, StringIO
from app.extensions import db
from app.models.producto import ProductoTerminado, Pieza, ColorProducto, FamiliaColor, ProductoPieza, Linea, Familia
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum


class ErrorSeverity(Enum):
    """Severidad del error de importación."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class ImportError:
    """Representa un error durante la importación."""
    fila: int
    columna: Optional[str]
    mensaje: str
    severidad: ErrorSeverity
    valor_original: Any = None
    
    def to_dict(self) -> dict:
        return {
            'fila': self.fila,
            'columna': self.columna,
            'mensaje': self.mensaje,
            'severidad': self.severidad.value,
            'valor_original': str(self.valor_original) if self.valor_original else None
        }


@dataclass
class ValidationResult:
    """Resultado de validación de archivo."""
    es_valido: bool = True
    total_filas: int = 0
    filas_validas: int = 0
    filas_con_errores: int = 0
    filas_con_warnings: int = 0
    errores: List[ImportError] = field(default_factory=list)
    warnings: List[ImportError] = field(default_factory=list)
    colores_nuevos: List[dict] = field(default_factory=list)      # Para Piezas: ColorProducto
    familias_nuevas: List[dict] = field(default_factory=list)     # Para Productos: FamiliaColor
    preview: List[dict] = field(default_factory=list)
    columnas_detectadas: List[str] = field(default_factory=list)
    columnas_faltantes: List[str] = field(default_factory=list)
    formato_archivo: str = ""
    
    def to_dict(self) -> dict:
        return {
            'es_valido': self.es_valido,
            'total_filas': self.total_filas,
            'filas_validas': self.filas_validas,
            'filas_con_errores': self.filas_con_errores,
            'filas_con_warnings': self.filas_con_warnings,
            'errores': [e.to_dict() for e in self.errores],
            'warnings': [w.to_dict() for w in self.warnings],
            'colores_nuevos': self.colores_nuevos,
            'familias_nuevas': self.familias_nuevas,
            'preview': self.preview,
            'columnas_detectadas': self.columnas_detectadas,
            'columnas_faltantes': self.columnas_faltantes,
            'formato_archivo': self.formato_archivo
        }


class ImportService:
    """Servicio para importar datos de catálogo desde Excel/CSV."""
    
    # Formatos soportados
    FORMATOS_SOPORTADOS = {
        'excel': ['.xlsx', '.xls'],
        'csv': ['.csv']
    }
    
    # Encodings a intentar para CSV
    CSV_ENCODINGS = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
    
    # Delimitadores comunes para CSV
    CSV_DELIMITERS = [',', ';', '\t', '|']
    
    # Mapeo de columnas Excel -> Modelo ProductoTerminado
    # NOTA: 'Cod Color' en CSV de productos = cod_familia_color (código de familia)
    # REFACTORIZADO: 'Cod Linea', 'Linea', 'Cod Familia', 'Familia' ya no se mapean - se usan para lookup de FK
    MAPEO_PRODUCTOS = {
        'Cod Producto': 'cod_producto',
        'Producto': 'producto',
        'Cod Color': 'cod_familia_color',  # RENOMBRADO: Es código de familia de color, no de color
        'Familia Color': 'familia_color',
        'COD SKU PT': 'cod_sku_pt',
        'UM': 'um',
        'Doc x Paq': 'doc_x_paq',
        'Doc x Bulto': 'doc_x_bulto',
        'PESO g.': 'peso_g',
        ' PESO g. ': 'peso_g',
        'Precio Estimado': 'precio_estimado',
        ' Precio Estimado ': 'precio_estimado',
        'Precio Sin IGV': 'precio_sin_igv',
        ' Precio Sin IGV ': 'precio_sin_igv',
        'Indicador x kg.': 'indicador_x_kg',
        ' Indicador x kg. ': 'indicador_x_kg',
        'Codigo Barra': 'codigo_barra',
        'Marca': 'marca',
        'Nombre GS1': 'nombre_gs1',
        'OBS': 'obs',
        'Status': 'status',
        'STATUS': 'status'
    }
    
    # Columnas requeridas para ProductoTerminado
    COLUMNAS_REQUERIDAS_PRODUCTOS = ['COD SKU PT', 'Producto']
    
    # Mapeo de columnas Excel -> Modelo Pieza
    # REFACTORIZADO: 'Cod Linea', 'Linea', 'FAMILIA' ya no se mapean - se usan para lookup de FK
    MAPEO_PIEZAS = {
        'SKU': 'sku',
        'Cod Pieza': 'cod_pieza',
        'PIEZAS': 'piezas',
        'Cod Col': 'cod_col',
        'Tipo Color': 'tipo_color',
        'Cavidad': 'cavidad',
        'Peso': 'peso',
        'Cod Extru': 'cod_extru',
        'Tipo Extruccion': 'tipo_extruccion',
        'Cod MP': 'cod_mp',
        'MP': 'mp',
        'Cod Color': 'cod_color',
        'Color': 'color'
    }
    
    # Columnas requeridas para Pieza
    COLUMNAS_REQUERIDAS_PIEZAS = ['SKU', 'PIEZAS']
    
    def __init__(self):
        self.errores: List[ImportError] = []
        self.warnings: List[ImportError] = []
    
    def detectar_formato(self, filename: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Detecta el formato del archivo basándose en la extensión.
        Returns: (formato, extension) o (None, None) si no es soportado
        """
        filename_lower = filename.lower()
        for formato, extensiones in self.FORMATOS_SOPORTADOS.items():
            for ext in extensiones:
                if filename_lower.endswith(ext):
                    return formato, ext
        return None, None
    
    def parsear_archivo(self, file_bytes: bytes, filename: str, tipo: str = 'productos') -> Tuple[Optional[pd.DataFrame], ValidationResult]:
        """
        Parsea archivo Excel o CSV y retorna DataFrame con resultado de validación inicial.
        
        Args:
            file_bytes: Contenido del archivo en bytes
            filename: Nombre del archivo (para detectar formato)
            tipo: 'productos' o 'piezas'
        
        Returns:
            Tuple de (DataFrame o None, ValidationResult)
        """
        result = ValidationResult()
        
        # Detectar formato
        formato, extension = self.detectar_formato(filename)
        if not formato:
            extensiones_validas = []
            for exts in self.FORMATOS_SOPORTADOS.values():
                extensiones_validas.extend(exts)
            result.es_valido = False
            result.errores.append(ImportError(
                fila=0,
                columna=None,
                mensaje=f"Formato de archivo no soportado. Use: {', '.join(extensiones_validas)}",
                severidad=ErrorSeverity.CRITICAL
            ))
            return None, result
        
        result.formato_archivo = formato
        df = None
        
        try:
            if formato == 'excel':
                df = self._parsear_excel(file_bytes, result)
            elif formato == 'csv':
                df = self._parsear_csv(file_bytes, result)
            
            if df is None:
                result.es_valido = False
                return None, result
            
            # Normalizar nombres de columnas
            df.columns = [self._normalizar_columna(col) for col in df.columns]
            result.columnas_detectadas = df.columns.tolist()
            
            # Verificar columnas requeridas
            columnas_requeridas = self.COLUMNAS_REQUERIDAS_PRODUCTOS if tipo == 'productos' else self.COLUMNAS_REQUERIDAS_PIEZAS
            for col_req in columnas_requeridas:
                col_normalizada = self._normalizar_columna(col_req)
                if col_normalizada not in [self._normalizar_columna(c) for c in df.columns]:
                    result.columnas_faltantes.append(col_req)
            
            if result.columnas_faltantes:
                result.es_valido = False
                result.errores.append(ImportError(
                    fila=0,
                    columna=None,
                    mensaje=f"Columnas requeridas no encontradas: {', '.join(result.columnas_faltantes)}",
                    severidad=ErrorSeverity.CRITICAL
                ))
                return df, result  # Retornamos df para que puedan ver qué columnas tiene
            
            result.total_filas = len(df)
            
            # Eliminar filas completamente vacías
            df = df.dropna(how='all')
            filas_vacias = result.total_filas - len(df)
            if filas_vacias > 0:
                result.warnings.append(ImportError(
                    fila=0,
                    columna=None,
                    mensaje=f"Se ignoraron {filas_vacias} filas completamente vacías",
                    severidad=ErrorSeverity.INFO
                ))
            
            result.total_filas = len(df)
            return df, result
            
        except Exception as e:
            result.es_valido = False
            result.errores.append(ImportError(
                fila=0,
                columna=None,
                mensaje=f"Error inesperado al leer archivo: {str(e)}",
                severidad=ErrorSeverity.CRITICAL
            ))
            return None, result
    
    def _parsear_excel(self, file_bytes: bytes, result: ValidationResult) -> Optional[pd.DataFrame]:
        """Parsea archivo Excel."""
        try:
            df = pd.read_excel(BytesIO(file_bytes))
            return df
        except Exception as e:
            result.errores.append(ImportError(
                fila=0,
                columna=None,
                mensaje=f"Error leyendo Excel: {str(e)}. ¿El archivo está corrupto o protegido?",
                severidad=ErrorSeverity.CRITICAL
            ))
            return None
    
    def _parsear_csv(self, file_bytes: bytes, result: ValidationResult) -> Optional[pd.DataFrame]:
        """Parsea archivo CSV intentando múltiples encodings y delimitadores."""
        
        # Intentar detectar encoding
        for encoding in self.CSV_ENCODINGS:
            try:
                content = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            result.errores.append(ImportError(
                fila=0,
                columna=None,
                mensaje=f"No se pudo detectar la codificación del archivo. Intentar guardar como UTF-8.",
                severidad=ErrorSeverity.CRITICAL
            ))
            return None
        
        # Intentar detectar delimitador
        for delimiter in self.CSV_DELIMITERS:
            try:
                df = pd.read_csv(StringIO(content), delimiter=delimiter)
                # Validar que tenga múltiples columnas
                if len(df.columns) > 1:
                    result.warnings.append(ImportError(
                        fila=0,
                        columna=None,
                        mensaje=f"CSV detectado con encoding '{encoding}' y delimitador '{delimiter}'",
                        severidad=ErrorSeverity.INFO
                    ))
                    return df
            except Exception:
                continue
        
        # Si ningún delimitador funcionó, intentar con el default
        try:
            df = pd.read_csv(StringIO(content))
            return df
        except Exception as e:
            result.errores.append(ImportError(
                fila=0,
                columna=None,
                mensaje=f"Error leyendo CSV: {str(e)}. Verifique el formato del archivo.",
                severidad=ErrorSeverity.CRITICAL
            ))
            return None
    
    def _normalizar_columna(self, col: str) -> str:
        """Normaliza nombre de columna: strip espacios, uppercase."""
        if isinstance(col, str):
            return col.strip().upper()
        return str(col).strip().upper()
    
    def validar_productos(self, df: pd.DataFrame) -> ValidationResult:
        """
        Valida datos de productos terminados con detalle de errores por fila.
        NOTA: ProductoTerminado usa FamiliaColor (SOLIDO, CARAMELO, TRANSPARENTE),
              no ColorProducto que son colores específicos como Rojo, Azul.
        """
        result = ValidationResult(
            total_filas=len(df),
            formato_archivo='validado',
            columnas_detectadas=df.columns.tolist()
        )
        
        # Para ProductoTerminado, buscamos FamiliaColor (no ColorProducto)
        familias_existentes = {f.nombre.upper(): f for f in FamiliaColor.query.all()}
        familias_nuevas_set = {}
        skus_vistos = set()
        
        for idx, row in df.iterrows():
            fila_num = idx + 2  # Excel/CSV es 1-indexed + header
            fila_valida = True
            
            # Validar SKU
            sku = self._obtener_valor_str(row, 'COD SKU PT')
            if not sku:
                result.errores.append(ImportError(
                    fila=fila_num,
                    columna='COD SKU PT',
                    mensaje="SKU vacío o inválido",
                    severidad=ErrorSeverity.ERROR,
                    valor_original=row.get('COD SKU PT')
                ))
                result.filas_con_errores += 1
                fila_valida = False
                continue
            
            # Validar SKU duplicado en archivo
            if sku in skus_vistos:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='COD SKU PT',
                    mensaje=f"SKU duplicado en el archivo: {sku}",
                    severidad=ErrorSeverity.WARNING,
                    valor_original=sku
                ))
                result.filas_con_warnings += 1
            skus_vistos.add(sku)
            
            # Validar Producto (nombre)
            producto = self._obtener_valor_str(row, 'Producto')
            if not producto:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='Producto',
                    mensaje="Nombre de producto vacío",
                    severidad=ErrorSeverity.WARNING
                ))
                result.filas_con_warnings += 1
            
            # Validar/Detectar FAMILIA de color (no color específico)
            # En ProductoTerminado: Cod Color es código de familia, Familia Color es nombre
            familia_color_nombre = self._obtener_valor_str(row, 'Familia Color')
            cod_color = self._obtener_valor_int(row, 'Cod Color')
            
            if familia_color_nombre:
                familia_upper = familia_color_nombre.upper()
                if familia_upper not in familias_existentes and familia_upper not in familias_nuevas_set:
                    familias_nuevas_set[familia_upper] = cod_color or 0
            
            # Validar campos numéricos
            peso = self._obtener_valor_float(row, 'PESO g.')
            if peso is not None and peso < 0:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='PESO g.',
                    mensaje=f"Peso negativo: {peso}",
                    severidad=ErrorSeverity.WARNING,
                    valor_original=peso
                ))
            
            if fila_valida:
                result.filas_validas += 1
        
        # Convertir familias nuevas a lista (para familias_nuevas, no colores_nuevos)
        for nombre, codigo in familias_nuevas_set.items():
            result.familias_nuevas.append({
                'nombre': nombre,
                'codigo': codigo
            })
        
        # Preview de primeras 10 filas - solo columnas que se importarán
        # Crear mapeo inverso: columna normalizada -> field del modelo
        col_to_field = {}
        for csv_col, model_field in self.MAPEO_PRODUCTOS.items():
            normalized = self._normalizar_columna(csv_col)
            if normalized not in col_to_field:  # Solo la primera ocurrencia
                col_to_field[normalized] = model_field
        
        # Mapear columnas del dataframe a fields
        df_col_to_field = {}
        for df_col in df.columns:
            normalized = self._normalizar_columna(df_col)
            if normalized in col_to_field:
                df_col_to_field[df_col] = col_to_field[normalized]
        
        for _, row in df.head(10).iterrows():
            row_data = {}
            for df_col, model_field in df_col_to_field.items():
                val = row.get(df_col)
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    row_data[model_field] = None
                elif isinstance(val, (int, float)):
                    row_data[model_field] = val
                else:
                    row_data[model_field] = str(val).strip() if val else None
            result.preview.append(row_data)
        
        result.es_valido = result.filas_con_errores == 0 and len(result.columnas_faltantes) == 0
        return result
    
    def validar_piezas(self, df: pd.DataFrame) -> ValidationResult:
        """
        Valida datos de piezas con detalle de errores por fila.
        """
        result = ValidationResult(
            total_filas=len(df),
            formato_archivo='validado',
            columnas_detectadas=df.columns.tolist()
        )
        
        colores_existentes = {c.codigo: c for c in ColorProducto.query.all()}
        colores_nuevos_dict = {}
        skus_vistos = set()
        
        for idx, row in df.iterrows():
            fila_num = idx + 2
            fila_valida = True
            
            # Validar SKU
            sku = self._obtener_valor_str(row, 'SKU')
            if not sku:
                result.errores.append(ImportError(
                    fila=fila_num,
                    columna='SKU',
                    mensaje="SKU vacío o inválido",
                    severidad=ErrorSeverity.ERROR,
                    valor_original=row.get('SKU')
                ))
                result.filas_con_errores += 1
                fila_valida = False
                continue
            
            # Validar SKU duplicado
            if sku in skus_vistos:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='SKU',
                    mensaje=f"SKU duplicado: {sku}",
                    severidad=ErrorSeverity.WARNING,
                    valor_original=sku
                ))
                result.filas_con_warnings += 1
            skus_vistos.add(sku)
            
            # Validar nombre pieza
            nombre = self._obtener_valor_str(row, 'PIEZAS')
            if not nombre:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='PIEZAS',
                    mensaje="Nombre de pieza vacío",
                    severidad=ErrorSeverity.WARNING
                ))
            
            # Validar cavidad
            cavidad = self._obtener_valor_int(row, 'Cavidad')
            if cavidad is not None and cavidad <= 0:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='Cavidad',
                    mensaje=f"Cavidad inválida (<=0): {cavidad}",
                    severidad=ErrorSeverity.WARNING,
                    valor_original=cavidad
                ))
            
            # Validar peso
            peso = self._obtener_valor_float(row, 'Peso')
            if peso is not None and peso < 0:
                result.warnings.append(ImportError(
                    fila=fila_num,
                    columna='Peso',
                    mensaje=f"Peso negativo: {peso}",
                    severidad=ErrorSeverity.WARNING,
                    valor_original=peso
                ))
            
            # Detectar color
            cod_color = self._obtener_valor_int(row, 'Cod Color')
            if cod_color is not None:
                if cod_color not in colores_existentes and cod_color not in colores_nuevos_dict:
                    nombre_color = self._obtener_valor_str(row, 'Color') or f"COLOR_{cod_color}"
                    colores_nuevos_dict[cod_color] = nombre_color.upper()
            
            if fila_valida:
                result.filas_validas += 1
        
        # Colores nuevos
        for cod, nombre in colores_nuevos_dict.items():
            result.colores_nuevos.append({
                'codigo': cod,
                'nombre': nombre
            })
        
        # Preview de primeras 5 filas - usar nombres del modelo
        col_to_field = {}
        for csv_col, model_field in self.MAPEO_PIEZAS.items():
            normalized = self._normalizar_columna(csv_col)
            if normalized not in col_to_field:
                col_to_field[normalized] = model_field
        
        df_col_to_field = {}
        for df_col in df.columns:
            normalized = self._normalizar_columna(df_col)
            if normalized in col_to_field:
                df_col_to_field[df_col] = col_to_field[normalized]
        
        for _, row in df.head(5).iterrows():
            row_data = {}
            for df_col, model_field in df_col_to_field.items():
                val = row.get(df_col)
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    row_data[model_field] = None
                elif isinstance(val, (int, float)):
                    row_data[model_field] = val
                else:
                    row_data[model_field] = str(val).strip() if val else None
            result.preview.append(row_data)
        
        result.es_valido = result.filas_con_errores == 0 and len(result.columnas_faltantes) == 0
        return result
    
    def _obtener_valor_str(self, row, columna: str) -> Optional[str]:
        """Obtiene valor string de una columna, buscando case-insensitive."""
        for col in row.index:
            if self._normalizar_columna(col) == self._normalizar_columna(columna):
                val = row[col]
                if pd.isna(val) or str(val).strip().lower() == 'nan':
                    return None
                return str(val).strip()
        return None
    
    def _obtener_valor_int(self, row, columna: str) -> Optional[int]:
        """Obtiene valor int de una columna."""
        val_str = self._obtener_valor_str(row, columna)
        if not val_str:
            return None
        try:
            return int(float(val_str))
        except (ValueError, TypeError):
            return None
    
    def _obtener_valor_float(self, row, columna: str) -> Optional[float]:
        """Obtiene valor float de una columna."""
        val_str = self._obtener_valor_str(row, columna)
        if not val_str:
            return None
        try:
            # Manejar formato con coma decimal
            val_str = val_str.replace(',', '.')
            return float(val_str)
        except (ValueError, TypeError):
            return None
    
    def ejecutar_import_productos(self, df: pd.DataFrame, crear_familias: bool = True) -> dict:
        """
        Ejecuta la importación de productos terminados.
        NOTA: Para productos, creamos FamiliaColor (SOLIDO, CARAMELO, etc.)
              no ColorProducto (que son colores específicos para Piezas).
        """
        resultado = {
            'productos_creados': 0,
            'productos_actualizados': 0,
            'familias_creadas': 0,
            'lineas_creadas': 0,
            'errores': [],
            'warnings': []
        }
        
        # Crear familias de color primero (no ColorProducto)
        # Indexamos por nombre Y código para evitar duplicados
        familias_existentes = {f.nombre.upper(): f for f in FamiliaColor.query.all()}
        familias_por_codigo = {f.codigo: f for f in FamiliaColor.query.all() if f.codigo is not None}
        
        if crear_familias:
            for _, row in df.iterrows():
                familia_nombre = self._obtener_valor_str(row, 'Familia Color')
                cod_familia = self._obtener_valor_int(row, 'Cod Color')
                
                if familia_nombre:
                    familia_upper = familia_nombre.upper()
                    # Verificar si ya existe por nombre O por código
                    existe_por_nombre = familia_upper in familias_existentes
                    existe_por_codigo = cod_familia is not None and cod_familia in familias_por_codigo
                    
                    if not existe_por_nombre and not existe_por_codigo:
                        nueva = FamiliaColor(
                            nombre=familia_upper,
                            codigo=cod_familia
                        )
                        db.session.add(nueva)
                        familias_existentes[familia_upper] = nueva
                        if cod_familia is not None:
                            familias_por_codigo[cod_familia] = nueva
                        resultado['familias_creadas'] += 1
                    elif existe_por_codigo and not existe_por_nombre:
                        # Si existe por código pero no por nombre, usamos la existente por código
                        familias_existentes[familia_upper] = familias_por_codigo[cod_familia]
            db.session.flush()
        
        # Crear/buscar Lineas (UPSERT)
        lineas_existentes = {l.nombre.upper(): l for l in Linea.query.all()}
        lineas_por_codigo = {l.codigo: l for l in Linea.query.all()}
        
        for _, row in df.iterrows():
            linea_nombre = self._obtener_valor_str(row, 'Linea')
            cod_linea = self._obtener_valor_int(row, 'Cod Linea')
            
            if linea_nombre:
                linea_upper = linea_nombre.upper()
                existe_por_nombre = linea_upper in lineas_existentes
                existe_por_codigo = cod_linea is not None and cod_linea in lineas_por_codigo
                
                if not existe_por_nombre and not existe_por_codigo:
                    nueva_linea = Linea(
                        nombre=linea_upper,
                        codigo=cod_linea if cod_linea else 0
                    )
                    db.session.add(nueva_linea)
                    lineas_existentes[linea_upper] = nueva_linea
                    if cod_linea is not None:
                        lineas_por_codigo[cod_linea] = nueva_linea
                    resultado['lineas_creadas'] += 1
                elif existe_por_codigo and not existe_por_nombre:
                    lineas_existentes[linea_upper] = lineas_por_codigo[cod_linea]
        db.session.flush()
        
        # Crear/buscar Familias (UPSERT) - Similar logic to Linea
        familias_producto_existentes = {f.nombre.upper(): f for f in Familia.query.all()}
        familias_producto_por_codigo = {f.codigo: f for f in Familia.query.all()}
        
        for _, row in df.iterrows():
            familia_nombre = self._obtener_valor_str(row, 'Familia')
            cod_familia = self._obtener_valor_int(row, 'Cod Familia')
            
            if familia_nombre:
                familia_upper = familia_nombre.upper()
                existe_por_nombre = familia_upper in familias_producto_existentes
                existe_por_codigo = cod_familia is not None and cod_familia in familias_producto_por_codigo
                
                if not existe_por_nombre and not existe_por_codigo:
                    nueva_familia = Familia(
                        nombre=familia_upper,
                        codigo=cod_familia if cod_familia else 0
                    )
                    db.session.add(nueva_familia)
                    familias_producto_existentes[familia_upper] = nueva_familia
                    if cod_familia is not None:
                        familias_producto_por_codigo[cod_familia] = nueva_familia
                    resultado['familias_producto_creadas'] = resultado.get('familias_producto_creadas', 0) + 1
                elif existe_por_codigo and not existe_por_nombre:
                    familias_producto_existentes[familia_upper] = familias_producto_por_codigo[cod_familia]
        db.session.flush()
        for idx, row in df.iterrows():
            try:
                sku = self._obtener_valor_str(row, 'COD SKU PT')
                if not sku:
                    continue
                
                producto_data = {}
                for excel_col, model_col in self.MAPEO_PRODUCTOS.items():
                    val = self._obtener_valor_str(row, excel_col)
                    if val:
                        if model_col in ['cod_producto', 'cod_familia_color', 'doc_x_paq', 'doc_x_bulto']:
                            producto_data[model_col] = self._obtener_valor_int(row, excel_col)
                        elif model_col in ['peso_g', 'precio_estimado', 'precio_sin_igv', 'indicador_x_kg']:
                            producto_data[model_col] = self._obtener_valor_float(row, excel_col)
                        else:
                            producto_data[model_col] = val
                
                # Asignar FK a FamiliaColor si existe
                familia_nombre = producto_data.get('familia_color')
                if familia_nombre and familia_nombre.upper() in familias_existentes:
                    producto_data['familia_color_id'] = familias_existentes[familia_nombre.upper()].id
                
                # Asignar FK a Linea (OBLIGATORIO) - leer directamente del CSV
                linea_nombre = self._obtener_valor_str(row, 'Linea')
                if linea_nombre and linea_nombre.upper() in lineas_existentes:
                    producto_data['linea_id'] = lineas_existentes[linea_nombre.upper()].id
                else:
                    # linea_id es requerido, usar HOGAR como default
                    if 'HOGAR' in lineas_existentes:
                        producto_data['linea_id'] = lineas_existentes['HOGAR'].id
                
                # Asignar FK a Familia (OBLIGATORIO) - leer directamente del CSV
                familia_nombre = self._obtener_valor_str(row, 'Familia')
                if familia_nombre and familia_nombre.upper() in familias_producto_existentes:
                    producto_data['familia_id'] = familias_producto_existentes[familia_nombre.upper()].id
                else:
                    # familia_id es requerido, buscar primera familia como default
                    if familias_producto_existentes:
                        primera_familia = next(iter(familias_producto_existentes.values()))
                        producto_data['familia_id'] = primera_familia.id
                
                # UPSERT: Verificar si existe
                existente = db.session.get(ProductoTerminado, sku)
                if existente:
                    # Actualizar (Saltar PK)
                    for key, value in producto_data.items():
                        if key == 'cod_sku_pt': continue
                        if hasattr(existente, key):
                            setattr(existente, key, value)
                    resultado['productos_actualizados'] += 1
                else:
                    # Crear
                    producto = ProductoTerminado(**producto_data)
                    db.session.add(producto)
                    resultado['productos_creados'] += 1
                
            except Exception as e:
                resultado['errores'].append({
                    'fila': idx + 2,
                    'mensaje': str(e)
                })
        
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            resultado['errores'].append({
                'fila': 0,
                'mensaje': f"Error al guardar en base de datos: {str(e)}"
            })
        
        return resultado
    
    def ejecutar_import_piezas(self, df: pd.DataFrame, crear_colores: bool = True) -> dict:
        """Ejecuta la importación de piezas."""
        resultado = {
            'piezas_creadas': 0,
            'piezas_actualizadas': 0,
            'colores_creados': 0,
            'lineas_creadas': 0,
            'errores': [],
            'warnings': []
        }
        
        # Crear colores primero
        colores_existentes = {c.codigo: c for c in ColorProducto.query.all()}
        if crear_colores:
            for _, row in df.iterrows():
                cod_color = self._obtener_valor_int(row, 'Cod Color')
                if cod_color and cod_color not in colores_existentes:
                    nombre = self._obtener_valor_str(row, 'Color') or f"COLOR_{cod_color}"
                    nuevo = ColorProducto(codigo=cod_color, nombre=nombre.upper())
                    db.session.add(nuevo)
                    colores_existentes[cod_color] = nuevo
                    resultado['colores_creados'] += 1
            db.session.flush()
        
        # Crear/buscar Lineas (UPSERT)
        lineas_existentes = {l.nombre.upper(): l for l in Linea.query.all()}
        lineas_por_codigo = {l.codigo: l for l in Linea.query.all()}
        
        for _, row in df.iterrows():
            linea_nombre = self._obtener_valor_str(row, 'Linea')
            cod_linea = self._obtener_valor_int(row, 'Cod Linea')
            
            if linea_nombre:
                linea_upper = linea_nombre.upper()
                existe_por_nombre = linea_upper in lineas_existentes
                existe_por_codigo = cod_linea is not None and cod_linea in lineas_por_codigo
                
                if not existe_por_nombre and not existe_por_codigo:
                    nueva_linea = Linea(
                        nombre=linea_upper,
                        codigo=cod_linea if cod_linea else 0
                    )
                    db.session.add(nueva_linea)
                    lineas_existentes[linea_upper] = nueva_linea
                    if cod_linea is not None:
                        lineas_por_codigo[cod_linea] = nueva_linea
                    resultado['lineas_creadas'] += 1
                elif existe_por_codigo and not existe_por_nombre:
                    lineas_existentes[linea_upper] = lineas_por_codigo[cod_linea]
        db.session.flush()
        
        # Crear/buscar Familias (UPSERT) - for Piezas too (reutilizar entidades existentes)
        familias_producto_existentes = {f.nombre.upper(): f for f in Familia.query.all()}
        familias_producto_por_codigo = {f.codigo: f for f in Familia.query.all()}
        
        for _, row in df.iterrows():
            familia_nombre = self._obtener_valor_str(row, 'FAMILIA')
            # Piezas no tienen 'Cod Familia', generamos código incremental
            if familia_nombre:
                familia_upper = familia_nombre.upper()
                existe_por_nombre = familia_upper in familias_producto_existentes
                
                if not existe_por_nombre:
                    # Generar código incremental
                    max_codigo = max(familias_producto_por_codigo.keys()) if familias_producto_por_codigo else 0
                    nueva_familia = Familia(
                        nombre=familia_upper,
                        codigo=max_codigo + 1
                    )
                    db.session.add(nueva_familia)
                    familias_producto_existentes[familia_upper] = nueva_familia
                    familias_producto_por_codigo[max_codigo + 1] = nueva_familia
                    resultado['familias_producto_creadas'] = resultado.get('familias_producto_creadas', 0) + 1
        db.session.flush()
        
        # Importar piezas
        for idx, row in df.iterrows():
            try:
                sku = self._obtener_valor_str(row, 'SKU')
                if not sku:
                    continue
                
                pieza_data = {}
                for excel_col, model_col in self.MAPEO_PIEZAS.items():
                    if model_col in ['cod_pieza', 'cavidad', 'cod_extru', 'cod_color']:
                        pieza_data[model_col] = self._obtener_valor_int(row, excel_col)
                    elif model_col == 'peso':
                        pieza_data[model_col] = self._obtener_valor_float(row, excel_col)
                    else:
                        pieza_data[model_col] = self._obtener_valor_str(row, excel_col)
                
                # Asignar color_id
                cod_color = pieza_data.get('cod_color')
                if cod_color and cod_color in colores_existentes:
                    pieza_data['color_id'] = colores_existentes[cod_color].id
                
                # Asignar linea_id (OBLIGATORIO) - leer directamente del CSV
                linea_nombre = self._obtener_valor_str(row, 'Linea')
                if linea_nombre and linea_nombre.upper() in lineas_existentes:
                    pieza_data['linea_id'] = lineas_existentes[linea_nombre.upper()].id
                else:
                    # linea_id es requerido, usar HOGAR como default
                    if 'HOGAR' in lineas_existentes:
                        pieza_data['linea_id'] = lineas_existentes['HOGAR'].id
                
                # Asignar familia_id (OBLIGATORIO) - leer directamente del CSV
                familia_nombre = self._obtener_valor_str(row, 'FAMILIA')
                if familia_nombre and familia_nombre.upper() in familias_producto_existentes:
                    pieza_data['familia_id'] = familias_producto_existentes[familia_nombre.upper()].id
                else:
                    # familia_id es requerido, buscar primera familia como default
                    if familias_producto_existentes:
                        primera_familia = next(iter(familias_producto_existentes.values()))
                        pieza_data['familia_id'] = primera_familia.id
                
                # UPSERT: Verificar si existe
                existente = db.session.get(Pieza, sku)
                if existente:
                    # Actualizar (Saltar PK)
                    for key, value in pieza_data.items():
                        if key == 'sku': continue
                        if hasattr(existente, key):
                            setattr(existente, key, value)
                    resultado['piezas_actualizadas'] += 1
                else:
                    # Crear
                    pieza = Pieza(**pieza_data)
                    db.session.add(pieza)
                    resultado['piezas_creadas'] += 1
                
            except Exception as e:
                resultado['errores'].append({
                    'fila': idx + 2,
                    'mensaje': str(e)
                })
        
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            resultado['errores'].append({
                'fila': 0,
                'mensaje': f"Error al guardar en base de datos: {str(e)}"
            })
        
        return resultado
