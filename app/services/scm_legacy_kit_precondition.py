from dataclasses import dataclass

from sqlalchemy import inspect, text


LEGACY_KIT_PRECONDITION_FAILED = "LEGACY_KIT_PRECONDITION_FAILED"


@dataclass(frozen=True)
class LegacyKitInspection:
    kit_count: int
    component_count: int
    kit_samples: tuple[str, ...]
    kit_column_exists: bool
    component_table_exists: bool

    @property
    def ready(self):
        return self.kit_count == 0 and self.component_count == 0

    @property
    def contract_applied(self):
        return (
            not self.kit_column_exists
            and not self.component_table_exists
        )

    def to_dict(self):
        return {
            "ready": self.ready,
            "kit_count": self.kit_count,
            "component_count": self.component_count,
            "kit_samples": list(self.kit_samples),
            "kit_column_exists": self.kit_column_exists,
            "component_table_exists": self.component_table_exists,
            "contract_applied": self.contract_applied,
        }


class LegacyKitPreconditionError(RuntimeError):
    code = LEGACY_KIT_PRECONDITION_FAILED

    def __init__(self, inspection):
        self.inspection = inspection
        super().__init__(
            "El contract de KIT legacy no puede ejecutarse: "
            f"{inspection.kit_count} KIT y "
            f"{inspection.component_count} componentes requieren conciliación."
        )


def inspect_legacy_kit_precondition(session, *, sample_limit=10):
    connection = session.connection()
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    component_table_exists = "pieza_componente" in tables
    piece_columns = {
        column["name"]
        for column in schema.get_columns("pieza_color")
    }
    kit_column_exists = "tipo" in piece_columns

    if kit_column_exists:
        kit_count = session.execute(text("""
            SELECT count(*)
            FROM pieza_color
            WHERE upper(trim(coalesce(tipo, ''))) IN ('KIT', 'COMPONENTE')
        """)).scalar_one()
        samples = tuple(session.execute(text("""
            SELECT sku
            FROM pieza_color
            WHERE upper(trim(coalesce(tipo, ''))) IN ('KIT', 'COMPONENTE')
            ORDER BY sku
            LIMIT :sample_limit
        """), {"sample_limit": sample_limit}).scalars())
    else:
        kit_count = 0
        samples = ()

    component_count = (
        session.execute(
            text("SELECT count(*) FROM pieza_componente")
        ).scalar_one()
        if component_table_exists
        else 0
    )
    return LegacyKitInspection(
        kit_count=int(kit_count or 0),
        component_count=int(component_count or 0),
        kit_samples=samples,
        kit_column_exists=kit_column_exists,
        component_table_exists=component_table_exists,
    )


def assert_legacy_kit_contract_ready(session):
    inspection = inspect_legacy_kit_precondition(session)
    if not inspection.ready:
        raise LegacyKitPreconditionError(inspection)
    return inspection
