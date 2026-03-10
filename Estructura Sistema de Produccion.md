# 📄 Informe de Definición de Estructura: Sistema de Producción (Completo y Normalizado)

Este documento define la estructura de datos, lógica de negocio y contrato de interfaz (API) para la vista completa de Órdenes de Producción.

> **Última actualización (refactoring):** Se eliminó el concepto de `tipo_orden` (estrategias Por Peso / Por Cantidad / Stock). Cada `LoteColor` ahora recibe un `meta_kg` directo como único input de producción. La snapshot del molde pasó de campos escalares a una tabla separada (`snapshot_composicion_molde`) que soporta moldes multi-pieza.

---

## 1. Entidad: Orden de Producción (Cabecera Global)

Es la entidad padre (`OrdenProduccion`). Contiene la configuración técnica de la máquina, el molde y los parámetros de producción.

| Atributo / Campo | Origen de Dato | Descripción | Fórmula / Lógica |
| :--- | :--- | :--- | :--- |
| **Nº OrdenProduccion** | Input (Sistema) | Identificador único (ej. OP-1322). Primary Key tipo String. | - |
| **Fecha Creación** | Automático | Fecha de registro en BD (Timestamp UTC). | `NOW()` |
| **F. Inicio** | **Input (Usuario)** | Fecha planificada para el arranque de producción. | - |
| **Producto (SKU)** | Input (Usuario) | FK a `ProductoTerminado`. Incluye campo `producto` como nombre en cache. | - |
| **Molde (Código)** | Input (Usuario) | FK a `Molde`. Incluye campo `molde` como nombre en cache. | - |
| **Máquina** | Input (Usuario) | FK a `Maquina`. | - |
| **Activa** | Automático | Estado de la orden (abierta/cerrada). Default: `true`. Una OP cerrada no permite crear nuevos Registros Diarios. | `Boolean` |
| **Snapshot T. Ciclo (seg)** | Input (Técnico) | Duración de un ciclo de inyección en segundos. | - |
| **Snapshot Horas Turno** | Input (Técnico) | Horas laborales por día (ej. 23 o 24). | - |
| **Snapshot Peso Colada (g)** | Input (Técnico) | Peso del ramal/runner (gramos). No incluye piezas. | - |
| **Ciclos** | Input (Técnico) | Cantidad de ciclos teóricos (opcional). | - |
| **T/C (Tipo Cambio)** | Input (Finanzas) | Tipo de cambio USD/PEN al crear la orden (para costeo). | - |

> **Eliminado:** El campo `tipo_orden` (estrategia Por Peso / Por Cantidad / Stock) fue removido en el refactoring. La única estrategia vigente es `meta_kg` directo por lote.

---

## 2. Entidad: Snapshot de Composición del Molde (Nueva Tabla)

Tabla `snapshot_composicion_molde`. Congela la configuración de piezas del molde **al momento de crear la OP**. Reemplaza los anteriores campos escalares `snapshot_cavidades` y `snapshot_peso_neto_gr` de la cabecera.

**Motivación:** Un molde puede producir distintos tipos de piezas simultáneamente (molde multi-pieza). Esta estructura permite registrar cada tipo de pieza con sus propias cavidades y peso unitario.

| Atributo | Tipo | Descripción |
| :--- | :--- | :--- |
| **id** | Auto (BD) | Primary Key. |
| **orden_id** | FK | Referencia al `numero_op` de la Orden padre. |
| **pieza_sku** | FK (nullable) | Referencia a `Pieza`. Nullable para override manual sin pieza en catálogo. |
| **cavidades** | Input | Número de cavidades para este tipo de pieza en el golpe. |
| **peso_unit_gr** | Input | Peso de una unidad de esta pieza (gramos). |
| **peso_subtotal_gr** | Calculado | `cavidades × peso_unit_gr` |

### Propiedades Derivadas (calculadas en `OrdenProduccion`)

| Propiedad | Fórmula |
| :--- | :--- |
| **peso_neto_golpe_gr** | `SUM(snapshot.peso_subtotal_gr)` para todos los snapshots de la OP |
| **peso_tiro_gr** | `peso_neto_golpe_gr + snapshot_peso_colada_gr` |
| **cavidades_totales** | `SUM(snapshot.cavidades)` |
| **es_multipieza** | `len(snapshot_composicion) > 1` |

---

## 3. Entidad: Lote de Color

Entidad hija (`LoteColor`). Cada lote representa un color de producción. Recibe `meta_kg` como **único input** de producción, eliminando el polimorfismo por estrategia.

| Atributo | Tipo | Descripción | Fórmula / Lógica |
| :--- | :--- | :--- | :--- |
| **Color** | Input (FK) | Referencia a `ColorProducto`. | - |
| **Producto SKU Output** | Input (FK, nullable) | Referencia al SKU de `ProductoTerminado` que produce este lote. | - |
| **meta_kg** | **Input directo** | Kilos objetivo para este color. Es el único input de producción del lote. | Usuario ingresa directamente |
| **Personas** | Input | Operarios asignados a la mezcla. Default: 1. | - |
| **calculo_coladas** | Calculado (persistido) | Golpes necesarios para cumplir la meta. Resultado exacto (Float, sin redondeo con ceil). | `(meta_kg × 1000) / peso_neto_golpe_gr` |
| **calculo_kg_real** | Calculado (persistido) | Kg reales que produce la máquina exactamente. Refleja el resultado de las coladas exactas. | `calculo_coladas × peso_neto_golpe_gr / 1000` |
| **calculo_horas_hombre** | Calculado (persistido) | Horas-Hombre proporcionales al tiempo total de la orden. | `(dias_orden × horas_turno × personas) / n_colores` |

> **Nota sobre `calculo_coladas`:** Es Float exacto sin `math.ceil`. La máquina opera con golpes: el sistema muestra el número exacto de golpes necesarios. Si se requiere un entero para planificación física, se aplica ceil en la capa de presentación.

> **Eliminado:** Los campos `Por Cantidad (Kg)`, `Peso (Kg)`, `Stock (Kg)` y `Extra (Kg)` fueron eliminados junto con el `tipo_orden`. La lógica polimórfica ya no existe.

---

## 4. Cálculos Cacheados en `OrdenProduccion`

Todos los valores calculados se **persisten en BD** y se actualizan llamando a `actualizar_metricas()`. Este método también dispara en cascada a los `LoteColor` hijos.

| Columna Persistida | Descripción | Fórmula |
| :--- | :--- | :--- |
| **calculo_peso_neto_golpe** | Peso neto total del golpe (piezas, sin colada). | `SUM(snap.cavidades × snap.peso_unit_gr)` |
| **calculo_peso_tiro_gr** | Peso total del golpe (piezas + ramal). | `peso_neto_golpe + snapshot_peso_colada_gr` |
| **calculo_cavidades_totales** | Total de cavidades del golpe. | `SUM(snap.cavidades)` |
| **calculo_colores_activos** | Número de lotes activos en la OP. | `len(lotes)` |
| **calculo_peso_produccion** | Meta neta total de producción. | `SUM(lote.meta_kg)` |
| **calculo_merma_pct** | % de merma (solo colada/runner). | `(peso_tiro - peso_neto) / peso_tiro` |
| **calculo_peso_inc_merma** | Producción incluyendo merma natural. | `calculo_peso_produccion × (1 + calculo_merma_pct)` |
| **calculo_merma_natural_kg** | Kilos físicos de desperdicio (colada). | `calculo_peso_inc_merma - calculo_peso_produccion` |
| **calculo_horas** | Tiempo estimado de inyección (horas). | `golpes × ciclo_seg / 3600` |
| **calculo_dias** | Tiempo estimado en días. | `calculo_horas / snapshot_horas_turno` |
| **calculo_fecha_fin** | Fecha estimada de finalización. | `fecha_inicio + timedelta(days=calculo_dias)` |
| **calculo_familia_color** | Cache del nombre de familia de color del producto. | Desde `ProductoTerminado.familia_color_rel` |

> **Nota sobre `calculo_merma_pct`:** La merma se calcula **únicamente** como el desperdicio físico del ramal/colada (runner), no como una merma de producción configurable. Es un dato objetivo del molde.

---

## 5. Entidad: Composición de Materiales (Lista Dinámica)

Define la mezcla de materia prima para cada lote. Tabla `se_compone`.

| Atributo | Origen | Descripción | Lógica |
| :--- | :--- | :--- | :--- |
| **Material** | Input (Select) | FK a `MateriaPrima`. | - |
| **Tipo** | Automático | Clasificación (Virgen, Segunda) traída del Material. | - |
| **Fracción** | Input (Manual) | Porcentaje de participación en la mezcla (0.0 a 1.0). | - |
| **calculo_peso_kg** | Calculado (persistido) | Kilos de material requeridos para este lote, incluyendo merma de colada. | `meta_kg × (1 + merma_pct) × fraccion` |

> **Validación:** La suma de las fracciones de la lista `materiales` debe ser **1.0**.

---

## 6. Entidad: Receta de Colorantes (Lista Dinámica)

Define la lista de pigmentos necesarios para el color. Tabla `se_colorea`.

| Atributo | Origen | Descripción | Lógica |
| :--- | :--- | :--- | :--- |
| **Pigmento** | Input (Select) | FK a `Colorante`. | - |
| **Dosis (gramos)** | Input (Manual) | Gramos por bolsa (dosis). | - |

---

## 7. Estructura JSON para Frontend (Referencia)

Estructura de respuesta del endpoint `GET /api/ordenes/<id>` (método `to_dict()`).

```json
{
  "numero_op": "OP-1322",
  "producto": "BALDE ROMANO",
  "maquina": "M1",
  "tipo_maquina": "Hidráulica 500T",
  "fecha": "2023-11-20T08:00:00",
  "fecha_inicio": "2023-11-21T07:00:00",
  "molde": "MOLDE-BALDE-01",
  "activa": true,

  "snapshot_tecnico": {
    "tiempo_ciclo_seg": 30.0,
    "horas_turno": 23.0,
    "peso_colada_gr": 2.0,
    "es_multipieza": false,
    "peso_neto_golpe_gr": 174.0,
    "peso_tiro_gr": 176.0,
    "cavidades_totales": 2,
    "composicion": [
      {
        "pieza_sku": "PIE-001",
        "pieza_nombre": "Balde Romano Cuerpo",
        "cavidades": 2,
        "peso_unit_gr": 87.0,
        "peso_subtotal_gr": 174.0
      }
    ]
  },

  "lotes": [
    {
      "id": 1,
      "Color": "Amarillo",
      "meta_kg": 175.0,
      "kg_real": 174.993,
      "coladas": 1006.8390,

      "materiales": [
        { "nombre": "PP Clarif", "tipo": "Virgen", "fraccion": 0.5, "peso_kg": 89.0 },
        { "nombre": "Molido",    "tipo": "Segunda", "fraccion": 0.5, "peso_kg": 89.0 }
      ],
      "pigmentos": [
        { "nombre": "Amarillo CH 1041", "dosis_gr": 30 },
        { "nombre": "Dioxido Titanio",  "dosis_gr": 5 }
      ],
      "mano_obra": {
        "personas": 1,
        "horas_hombre": 18.3
      }
    }
  ],

  "resumen_totales": {
    "Peso(Kg) PRODUCCION": 175.0,
    "Peso (Kg) Inc. Merma": 177.0,
    "%Merma": 0.0114,
    "Merma Natural Kg": 2.0,
    "Horas": 13.24,
    "Días": 0.58,
    "F. Fin": "2023-11-21T23:27:00",
    "Familia Color": "BALDE"
  },

  "avance_real_kg": 0.0,
  "avance_real_coladas": 0
}
```

---

## 8. Entidad: Registro Diario de Producción (Hoja de Producción)

Representa la "Hoja de Producción" física que se llena por turno. Es hija de `OrdenProduccion` y contiene la producción real reportada por los maquinistas.

| Atributo / Campo | Origen de Dato | Descripción | Fórmula / Lógica |
| :--- | :--- | :--- | :--- |
| **ID Registro** | Auto (BD) | Identificador único del registro diario. | `AUTOINCREMENT` |
| **Orden ID (FK)** | Selección | Referencia a la Orden de Producción padre. | - |
| **Máquina ID (FK)** | Selección | Máquina donde se ejecutó la producción. | - |
| **Fecha** | Input (Manual) | Fecha del turno de producción. | - |
| **Turno** | Input (Select) | DIURNO, NOCTURNO, o EXTRA. | - |
| **Hora Inicio** | Input (Manual) | Hora de arranque (ej. 07:00). | - |
| **Colada Inicial** | Input (Manual) | Contador de la máquina al inicio del turno. | - |
| **Colada Final** | Input (Manual) | Contador de la máquina al final del turno. | - |
| **Tiempo Ciclo Reportado** | Input (Manual) | T/C observado en el panel (segundos). | - |
| **Tiempo Enfriamiento** | Input (Manual) | Tiempo de enfriamiento observado (seg). | - |

### Snapshots del Registro (Captura al Crear)

Se copian de la `OrdenProduccion` al momento de crear el registro para mantener consistencia histórica. A diferencia de la OP, el registro mantiene snapshots escalares (ya que un turno opera sobre el molde completo).

| Atributo | Origen | Descripción |
| :--- | :--- | :--- |
| **snapshot_cavidades** | Orden | Total de cavidades del golpe al crear el registro. |
| **snapshot_peso_neto_gr** | Orden | Peso neto total del golpe (todas las piezas, gramos). |
| **snapshot_peso_colada_gr** | Orden | Peso del ramal/colada (gramos). |

### Totalizadores (Calculados)

| Atributo | Descripción | Fórmula | Prioridad |
| :--- | :--- | :--- | :--- |
| **total_coladas_calculada** | Ciclos realizados en el turno. | `colada_final - colada_inicial` | Contadores > Suma detalles |
| **total_piezas_buenas** | Piezas buenas producidas. | `total_coladas_calculada × snapshot_cavidades` | - |
| **total_kg_real** | Kg reales del turno. | `SUM(ControlPeso.peso_real_kg)` | Pesajes reales > Cálculo coladas |

> **Nota sobre `total_kg_real`:** Prioridad 1: suma de pesajes físicos (`ControlPeso`). Prioridad 2 (fallback): `total_coladas × (peso_neto_gr + peso_colada_gr) / 1000`.

---

## 9. Entidad: Detalle Producción Hora (Tabla Interna)

Cada `RegistroDiarioProduccion` tiene N filas de detalle, una por cada hora trabajada. Permite el seguimiento hora-a-hora.

| Atributo | Origen | Descripción |
| :--- | :--- | :--- |
| **Hora** | Auto | Franja horaria (ej. "07:00 - 08:00"). |
| **Maquinista** | Input | Nombre del operador en esa hora. |
| **Color** | Input | Color producido (puede cambiar por hora). |
| **Coladas Realizadas** | Input | Cantidad de ciclos en esa hora. |
| **Observación** | Input | Notas (parada, cambio de molde, etc.). |
| **Cantidad Piezas** | Calculado | `coladas_realizadas × cavidades` |
| **Kg Producidos** | Calculado | `(coladas_realizadas × peso_tiro_gr) / 1000` |

---

## 10. Entidad: Control de Peso (Doble Verificación)

Sistema de pesaje individual de "bultos" para contrastar con la producción reportada. Sirve como doble verificación y control de calidad.

| Atributo | Origen | Descripción |
| :--- | :--- | :--- |
| **ID** | Auto | Identificador del pesaje. |
| **Registro ID (FK)** | Sistema | Vinculado al Registro Diario padre. |
| **Peso Real (Kg)** | Input/Balanza | Peso medido del bulto. |
| **Color** | Input | Color o identificador del bulto. |
| **Hora Registro** | Automático | Timestamp del pesaje. |

### Validación de Peso

| Métrica | Descripción | Fórmula |
| :--- | :--- | :--- |
| **Total Pesado** | Suma de todos los bultos. | `SUM(peso_real_kg)` |
| **Peso Teórico** | Peso calculado del registro. | `registro.total_kg_real` |
| **Diferencia** | Discrepancia entre ambos. | `total_pesado - peso_teorico` |
| **Coincide** | Validación con tolerancia. | `ABS(diferencia) < 5 Kg` |

---

## 11. Estructura JSON: Registro Diario (Referencia)

Estructura de respuesta para `GET /api/ordenes/<op>/registros`.

```json
{
  "id": 1,
  "fecha": "2023-11-21",
  "turno": "DIURNO",
  "maquina": "INY-05",
  "orden": "OP-1322",
  "contadores": {
    "inicial": 1000,
    "final": 1500,
    "total": 500
  },
  "parametros": {
    "ciclo": 30.0,
    "enfriamiento": 5.0
  },
  "totales_estimados": {
    "piezas": 1000,
    "kg_total": 88.0
  },
  "detalles": [
    {
      "hora": "07:00",
      "maquinista": "Juan Perez",
      "color": "AMARILLO",
      "coladas": 50,
      "piezas": 100,
      "kg": 8.8
    }
  ]
}
```

---

## 12. Resumen de Cambios del Refactoring

| Aspecto | Antes | Después |
| :--- | :--- | :--- |
| **Estrategia de meta** | `tipo_orden`: Por Peso / Por Cantidad / Stock | Eliminado. `meta_kg` directo por lote |
| **Input de LoteColor** | Campo polimórfico según `tipo_orden` | `meta_kg` único campo de input |
| **Snapshot molde** | Campos escalares en `OrdenProduccion` (`snapshot_cavidades`, `snapshot_peso_neto_gr`) | Tabla `snapshot_composicion_molde` (soporta multi-pieza) |
| **Cálculo coladas** | Con `math.ceil` (entero) | Float exacto sin redondeo |
| **calculo_kg_real** | Aproximación por ceil | `coladas_float × peso_neto_golpe / 1000` (resultado exacto) |
| **Merma** | Configurable / multiple fuentes | Solo merma física de colada (runner): `(tiro - neto) / tiro` |
| **Campos eliminados** | `Extra (Kg)`, `TOTAL + EXTRA`, `%EXTRA`, `Peso(Kg) PRODUCCION` polimórfico | Todos eliminados |