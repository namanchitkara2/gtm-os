"""Shared typed models for GTM-OS.

Every other package depends on this one and it depends on nothing but pydantic.
Keep it that way: a schema package that imports a provider is a circular import
waiting to happen the first time a provider wants to return a typed row.
"""

from gtmos_schemas.evidence import Evidence, Sourced
from gtmos_schemas.entities import (
    Company,
    Education,
    Employment,
    Person,
    PersonIdentity,
)
from gtmos_schemas.targets import ICP, RoleSpec, Target, TargetKind
from gtmos_schemas.runs import (
    Run,
    RunStatus,
    RunStep,
    StepStatus,
    WorkflowRef,
)
from gtmos_schemas.bridge import (
    WRITE_ACTIONS,
    BridgeAck,
    BridgeDispatch,
    BridgeEnvelope,
    BridgeHello,
    BridgeLedgerQuery,
    BridgeLedgerReply,
    BridgeReject,
    BridgeResult,
    BridgeStatus,
)

__all__ = [
    "Evidence",
    "Sourced",
    "Company",
    "Education",
    "Employment",
    "Person",
    "PersonIdentity",
    "ICP",
    "RoleSpec",
    "Target",
    "TargetKind",
    "Run",
    "RunStatus",
    "RunStep",
    "StepStatus",
    "WorkflowRef",
    "BridgeAck",
    "BridgeDispatch",
    "BridgeEnvelope",
    "BridgeHello",
    "BridgeResult",
    "BridgeStatus",
    "BridgeLedgerQuery",
    "BridgeLedgerReply",
    "BridgeReject",
    "WRITE_ACTIONS",
]
