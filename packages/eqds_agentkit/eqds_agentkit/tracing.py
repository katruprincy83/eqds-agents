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
def span(name: str, span_type: str = "CHAIN", inputs: Any = None, **attrs: Any):
    """A span that is also a no-op when MLflow is missing or misconfigured: tracing must never fail a request. Exceptions from the
    body propagate (and are recorded on the span)."""
    stack = contextlib.ExitStack()
    try:
        import mlflow

        s = stack.enter_context(mlflow.start_span(name=name, span_type=span_type))
    except Exception:  # noqa: BLE001
        s = None
    with stack:
        if s is not None:
            try:
                if inputs is not None:
                    s.set_inputs(inputs)
                s.set_attributes({k: v for k, v in attrs.items() if v is not None})
            except Exception:  # noqa: BLE001
                pass
        yield s


def tag_trace(user_id: Any = None, conversation_id: Any = None, **tags: Any) -> str | None:
    """Tag the active trace and return its id. Call it inside the root span.

    Tags (searchable): conversation_id, run_id, user_id, caller, app_version, rounds, specialists ...
    Metadata: `mlflow.trace.session` = conversation_id and `mlflow.trace.user` = user_id, so the Traces UI groups a conversation
    into a session and filters by user.
    """
    try:
        import mlflow

        clean = {k: str(v) for k, v in {**tags, "user_id": user_id, "conversation_id": conversation_id}.items() if v not in (None, "")}
        clean.setdefault("app_version", os.environ.get("EQDS_APP_VERSION", "dev"))
        meta = {}
        if conversation_id:
            meta["mlflow.trace.session"] = str(conversation_id)
        if user_id:
            meta["mlflow.trace.user"] = str(user_id)
        try:
            mlflow.update_current_trace(tags=clean, metadata=meta or None)
        except TypeError:  # older MLflow without `metadata`
            mlflow.update_current_trace(tags=clean)
        return mlflow.get_current_active_span().trace_id
    except Exception:  # noqa: BLE001
        return None


def current_trace_id() -> str | None:
    """Id of the trace the caller is inside (None outside a trace). Sent to specialists so their trace can point back."""
    try:
        import mlflow

        s = mlflow.get_current_active_span()
        return s.trace_id if s is not None else None
    except Exception:  # noqa: BLE001
        return None


def preview(request: str | None = None, response: str | None = None) -> None:
    """Set the request / response text shown in the Traces table for the active trace."""
    try:
        import mlflow

        mlflow.update_current_trace(request_preview=(request or "")[:1000] or None, response_preview=(response or "")[:1000] or None)
    except Exception:  # noqa: BLE001
        pass


def log_feedback(trace_id: str, value: bool, comment: str = "", user_id: str = "") -> None:
    import mlflow
    from mlflow.entities import AssessmentSource, AssessmentSourceType

    mlflow.log_feedback(trace_id=trace_id, name="user_rating", value=value, rationale=comment or None,
                        source=AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id=user_id or "unknown"))
