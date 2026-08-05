"""Auditoría read-only previa al contract destructivo de KIT legacy."""

import json

from app import create_app
from app.extensions import db
from app.services.scm_legacy_kit_precondition import (
    LegacyKitPreconditionError,
    assert_legacy_kit_contract_ready,
)


def main():
    app = create_app()
    with app.app_context():
        try:
            inspection = assert_legacy_kit_contract_ready(db.session)
        except LegacyKitPreconditionError as error:
            print(json.dumps({
                "code": error.code,
                **error.inspection.to_dict(),
            }, ensure_ascii=False))
            return 2
        print(json.dumps({
            "code": "LEGACY_KIT_PRECONDITION_OK",
            **inspection.to_dict(),
        }, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
