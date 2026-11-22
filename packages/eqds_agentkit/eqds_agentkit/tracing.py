"""Tracing conventions (design §11.1). All helpers degrade to no-ops when MLflow is not configured."""
from __future__ import annotations

import contextlib
import os
from typing import Any

_enabled = False


def setup(app_version: str = "dev", experiment_id: str | None = None, autolog: bool = True) -> bool:
    """Point MLflow at the workspace experiment (MLFLOW_EXPERIMENT_ID env var) and enable LangChain autolog."""
    global _enabled
    try:
        import mlflow

        mlflow.set_tracking_uri("databricks")
        eid = experiment_id or os.environ.get("MLFLOW_EXPERIMENT_ID")
        if eid:
            mlflow.set_experiment(experiment_id=eid)
        if autolog:
            try:
                mlflow.langchain.autolog()
            except Exception:  # noqa: BLE001
                pass
        os.environ.setdefault("EQDS_APP_VERSION", app_version)
        _enabled = bool(eid)
    except Exception:  # noqa: BLE001
        _enabled = False
    return _enabled


@contextlib.contextmanager
def span(name: str, span_type: str = "CHAIN", **attrs: Any):
    try:
        import mlflow

        with mlflow.start_span(name=name, span_type=span_type) as s:
            for k, v in attrs.items():
                try:
                    s.set_attribute(k, v)
                except Exception:  # noqa: BLE001
                    pass
            yield s
    except ImportError:
        yield None


def tag_trace(**tags: Any) -> str | None:
    """Tag the active trace (conversation_id, run_id, user_id, caller, app_version, rounds, specialists) and return its id."""
    try:
        import mlflow

        clean = {k: str(v) for k, v in tags.items() if v is not None}
        clean.setdefault("app_version", os.environ.get("EQDS_APP_VERSION", "dev"))
        mlflow.update_current_trace(tags=clean)
        return mlflow.get_current_active_span().trace_id
    except Exception:  # noqa: BLE001
        return None


def log_feedback(trace_id: str, value: bool, comment: str = "", user_id: str = "") -> None:
    import mlflow
    from mlflow.entities import AssessmentSource, AssessmentSourceType

    mlflow.log_feedback(trace_id=trace_id, name="user_rating", value=value, rationale=comment or None,
                        source=AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id=user_id or "unknown"))
