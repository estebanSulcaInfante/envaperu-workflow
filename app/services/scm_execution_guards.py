"""Shared fail-closed guards for writes competing with OF execution control."""

from sqlalchemy import select

from app.models.scm_production_orders import ScmOrdenOperacion
from app.services.scm_service_support import ScmServiceError


def ensure_of_accepts_execution_write(session, operation_id, *, action="WRITE"):
    """Reject new executable work while an aggregate OF is stopped.

    Callers still own their normal capability, version and idempotency checks;
    this guard only prevents a stopped OF from being resurrected by a second
    endpoint.
    """
    if operation_id is None:
        return None
    # The OF row is the aggregate serialization point.  Every competing
    # write must take the same lock before checking PAUSADA/CANCELADA so a
    # lifecycle command cannot race an insertion after its preflight.
    order = session.scalar(
        select(ScmOrdenOperacion)
        .where(ScmOrdenOperacion.id == operation_id)
        .with_for_update(of=ScmOrdenOperacion)
    )
    if order is None or order.tipo != "FABRICACION":
        return order
    if order.estado in {"PAUSADA", "CANCELADA"}:
        raise ScmServiceError(
            "OF_EXECUTION_STOPPED",
            f"La OF esta {order.estado.lower()}; no se puede {str(action).lower()} hasta reanudarla o conciliarla.",
            status_code=409,
            details={"estado": order.estado, "operation_id": str(operation_id)},
        )
    return order
