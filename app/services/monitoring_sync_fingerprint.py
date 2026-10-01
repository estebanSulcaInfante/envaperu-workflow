"""Canonical state hashes shared by the optional monitoring sync contract."""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal


def _dimension(value):
    return " ".join(str(value).strip().split()).upper() or None if value is not None else None


def _timestamp(value, *, local=False):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=-5)) if local else timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _date(value):
    return value.isoformat() if isinstance(value, date) else date.fromisoformat(value).isoformat()


def _hash(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def progress_fingerprint(payload):
    rows = []
    for row in payload["rows"]:
        rows.append({
            "operational_date": _date(row["operational_date"]),
            **{key: _dimension(row[key]) for key in ("op", "ot", "mold", "color", "machine_code", "shift")},
            "bags": int(row["bags"]),
            "weight_kg": format(Decimal(str(row["weight_kg"])).quantize(Decimal("0.001")), "f"),
            "first_capture_at_utc": _timestamp(row["first_capture_at_utc"]),
            "last_capture_at_utc": _timestamp(row["last_capture_at_utc"]),
        })
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return _hash({"window_start_date": _date(payload["window_start_date"]),
                  "window_end_date": _date(payload["window_end_date"]),
                  "timezone": "America/Lima", "source": "LOCAL_REPORTED_LEGACY", "rows": rows})


def closure_fingerprint(closures):
    rows = [{"op": str(row["op"]).strip(), "mold": row["mold"],
             "reason": row["reason"], "closed_at_utc": _timestamp(row["closed_at_local"], local=True)}
            for row in closures]
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return _hash(rows)
