from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.services.poller import SupervisorState
from app.utils.time import now_utc


@dataclass
class HealthReport:
    app_alive: bool
    db_ok: bool
    supervisor_alive: bool
    last_probe_at: datetime | None
    seconds_since_last_probe: float | None
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_health_report(state: SupervisorState, db_ok: bool) -> HealthReport:
    seconds_since_last_probe = None
    if state.last_probe_at is not None:
        seconds_since_last_probe = (now_utc() - state.last_probe_at).total_seconds()

    return HealthReport(
        app_alive=True,
        db_ok=db_ok,
        supervisor_alive=state.running,
        last_probe_at=state.last_probe_at,
        seconds_since_last_probe=seconds_since_last_probe,
        details={
            "iteration_count": state.iteration_count,
            "last_error": state.last_error,
            "active_recorder_count": state.active_recorder_count,
        },
    )
