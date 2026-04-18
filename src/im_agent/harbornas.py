from __future__ import annotations

import warnings

from im_agent.harborbeacon import (
    HarborBeaconTaskClient,
    HarborNASTaskClient,
    TaskTurnResult,
    build_harborbeacon_client_from_env,
    build_harbornas_client_from_env,
    build_task_request,
    derive_route_key,
    derive_session_id,
)

warnings.warn(
    "im_agent.harbornas is deprecated; prefer im_agent.harborbeacon",
    FutureWarning,
    stacklevel=2,
)

__all__ = [
    "HarborBeaconTaskClient",
    "HarborNASTaskClient",
    "TaskTurnResult",
    "build_harborbeacon_client_from_env",
    "build_harbornas_client_from_env",
    "build_task_request",
    "derive_route_key",
    "derive_session_id",
]
