"""Resolve effective manga attribution without rewriting physical facts."""

from datetime import timezone


def assignment_correction(manga):
    return getattr(manga, "correccion_asignacion", None)


def effective_work(manga):
    correction = assignment_correction(manga)
    segment = effective_segment(manga)
    # A real segment created after the correction is the next physical
    # responsibility hand-off.  The overlay remains the answer only until
    # that hand-off exists; never let it hide a later ledger fact.
    if correction is not None and not _segment_supersedes_correction(segment, correction):
        return correction.destino_trabajo
    return segment.trabajo if segment is not None else manga.trabajo


def effective_work_for_segment(manga, segment):
    """Resolve the owner of one physical ledger segment.

    Assignment correction is anchored to exactly one existing segment.  A
    later real segment is independent and keeps its own Trabajo, even when
    the manga's current projection has moved again.
    """
    correction = assignment_correction(manga)
    target_id = getattr(correction, "tramo_objetivo_id", None)
    if target_id is None and getattr(correction, "tramo_objetivo", None) is not None:
        target_id = correction.tramo_objetivo.id
    if (
        correction is not None
        and segment is not None
        and target_id == segment.id
    ):
        return correction.destino_trabajo
    return segment.trabajo if segment is not None else None


def effective_assignment(manga):
    correction = assignment_correction(manga)
    segment = effective_segment(manga)
    if correction is not None and not _segment_supersedes_correction(segment, correction):
        return correction.destino_asignacion
    return segment.asignacion_personal_trabajo if segment is not None else manga.asignacion_personal_trabajo


def effective_assignment_for_segment(manga, segment):
    correction = assignment_correction(manga)
    target_id = getattr(correction, "tramo_objetivo_id", None)
    if target_id is None and getattr(correction, "tramo_objetivo", None) is not None:
        target_id = correction.tramo_objetivo.id
    if (
        correction is not None
        and segment is not None
        and target_id == segment.id
    ):
        return correction.destino_asignacion
    return segment.asignacion_personal_trabajo if segment is not None else None


def effective_plan_assignment(manga):
    correction = assignment_correction(manga)
    segment = effective_segment(manga)
    if correction is not None and not _segment_supersedes_correction(segment, correction):
        return correction.destino_asignacion_plan
    if segment is not None and segment.asignacion_plan_id is not None:
        return segment.asignacion_plan
    return manga.asignacion


def effective_plan_assignment_for_segment(manga, segment):
    correction = assignment_correction(manga)
    target_id = getattr(correction, "tramo_objetivo_id", None)
    if target_id is None and getattr(correction, "tramo_objetivo", None) is not None:
        target_id = correction.tramo_objetivo.id
    if (
        correction is not None
        and segment is not None
        and target_id == segment.id
    ):
        return correction.destino_asignacion_plan
    if segment is not None and segment.asignacion_plan_id is not None:
        return segment.asignacion_plan
    return manga.asignacion


def effective_segment(manga):
    correction = assignment_correction(manga)
    latest = _latest_segment(manga)
    if correction is not None and correction.tramo_objetivo is not None:
        if latest is None or latest.secuencia <= correction.tramo_objetivo.secuencia:
            return correction.tramo_objetivo
    return latest


def _segment_supersedes_correction(segment, correction):
    target = getattr(correction, "tramo_objetivo", None)
    if segment is not None and target is None:
        segment_created_at = getattr(segment, "created_at", None)
        correction_created_at = getattr(correction, "created_at", None)
        return (
            segment_created_at is not None
            and correction_created_at is not None
            and _comparable_datetime(segment_created_at)
            > _comparable_datetime(correction_created_at)
        )
    return (
        segment is not None
        and target is not None
        and segment.id != target.id
        and segment.secuencia > target.secuencia
    )


def _comparable_datetime(value):
    """Normalize SQLite-naive and PostgreSQL-aware UTC timestamps."""
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _latest_segment(manga):
    segments = list(getattr(manga, "tramos_trabajo", ()) or ())
    return max(segments, key=lambda item: item.secuencia) if segments else None


def effective_identity(manga):
    correction = assignment_correction(manga)
    work = effective_work(manga)
    assignment = effective_assignment(manga)
    segment = effective_segment(manga)
    return {
        "trabajo_ot_id": str(work.id) if work is not None else None,
        "trabajo_codigo": work.codigo if work is not None else None,
        "asignacion_personal_trabajo_id": str(assignment.id) if assignment is not None else None,
        "tramo_id": str(segment.id) if segment is not None else None,
        "correccion_asignacion_id": str(correction.public_id) if correction is not None else None,
    }
