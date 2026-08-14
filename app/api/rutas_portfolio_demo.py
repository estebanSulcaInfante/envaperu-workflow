"""Public controls for the isolated portfolio environment."""

from threading import Lock

from flask import Blueprint, current_app, jsonify

from app.extensions import db
from app.services.scm_portfolio_demo_service import (
    PortfolioDemoError,
    portfolio_demo_status,
    prepare_portfolio_demo,
)


portfolio_demo_bp = Blueprint("portfolio_demo", __name__)
_reset_lock = Lock()


@portfolio_demo_bp.get("/status")
def get_portfolio_demo_status():
    return jsonify(portfolio_demo_status(db.session))


@portfolio_demo_bp.post("/reset")
def reset_portfolio_demo():
    if not _reset_lock.acquire(blocking=False):
        return jsonify({
            "error": "La demo ya se esta reiniciando.",
            "code": "DEMO_RESET_IN_PROGRESS",
        }), 409
    try:
        result = prepare_portfolio_demo(
            database_url=current_app.config["SQLALCHEMY_DATABASE_URI"],
            demo_mode=current_app.config["SCM_DEMO_MODE"],
        )
        return jsonify(result)
    except PortfolioDemoError as error:
        return jsonify({"error": str(error), "code": "DEMO_RESET_BLOCKED"}), 409
    finally:
        _reset_lock.release()
