"""
CompIntel offer workflow (PRD MVP): company metrics -> benchmark -> package ->
checks -> approvals -> briefs -> outcome capture. Mounted at /app.

Runs as a clearly labelled demo: benchmarks come from the synthetic,
benchmark-calibrated dataset the existing predictor was trained on.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from . import db
from . import realtime_routes  # noqa: F401  (adds /app/market and /app/integrations to bp)
from .api_v1 import bp as api_v1_bp
from .benchmark import CohortIndex
from .routes import bp, configure
from .streaming import pipeline


def _secret_key(app) -> str:
    env = os.environ.get("COMPINTEL_SECRET_KEY")
    if env:
        return env
    path = Path(app.root_path) / "instance" / "secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    return path.read_text().strip()


def register_workflow(app, *, predict_fn, model_version: str, stage_default_health: dict, dataset_path: Path,
                      predict_batch=None):
    if not app.secret_key:
        app.secret_key = _secret_key(app)
    app.config.setdefault("COMPINTEL_DB", os.environ.get("COMPINTEL_DB"))
    # Demo sign-in (the user switcher) only runs when explicitly enabled.
    app.config.setdefault("COMPINTEL_DEMO_MODE",
                          os.environ.get("COMPINTEL_DEMO_MODE", "").strip().lower() in ("1", "true", "yes"))
    app.config["STAGE_DEFAULT_HEALTH"] = stage_default_health
    app.teardown_appcontext(db.close_db)
    db.init_db(app)
    with app.app_context():
        from .seed import upgrade_demo_workspace
        for change in upgrade_demo_workspace():
            logging.getLogger(__name__).info("Demo workspace upgraded: %s", change)
        db.close_db()
    configure(predict_fn, CohortIndex(dataset_path, predict_batch=predict_batch), model_version)
    app.register_blueprint(bp)
    app.register_blueprint(api_v1_bp)
    pipeline.init_app(app)
