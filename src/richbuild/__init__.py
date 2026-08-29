"""RICH: typed foundations for intent-to-software compilation."""

from importlib import metadata as _metadata


def _version() -> str:
    """The installed distribution's version, or an honest marker for a checkout."""

    try:
        return _metadata.version("rich-agent-build-system")
    except _metadata.PackageNotFoundError:
        return "0.0.0+source"


__version__ = _version()

from .models import (
    SCHEMA_VERSION,
    AcceptanceAction,
    AcceptanceScenario,
    AcceptanceStep,
    Approval,
    ApprovalGate,
    ApprovalStatus,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    BuildRun,
    BuildTask,
    BrowserLocator,
    BrowserLocatorKind,
    Contract,
    EdgeKind,
    ErrorContract,
    Evidence,
    EvidenceKind,
    EvidenceStatus,
    Invariant,
    ModelValidationError,
    NodeKind,
    OperationContract,
    PortDirection,
    PortSpec,
    ProjectSpec,
    Requirement,
    RequirementKind,
    RequirementPriority,
    RunStatus,
    TaskKind,
    TaskStatus,
    UnsupportedSchemaVersion,
    validate_release_traceability,
)

__all__ = [
    "SCHEMA_VERSION",
    "AcceptanceAction",
    "AcceptanceScenario",
    "AcceptanceStep",
    "Approval",
    "ApprovalGate",
    "ApprovalStatus",
    "ArchitectureEdge",
    "ArchitectureNode",
    "ArchitectureSpec",
    "Artifact",
    "ArtifactKind",
    "ArtifactStatus",
    "BuildRun",
    "BuildTask",
    "BrowserLocator",
    "BrowserLocatorKind",
    "Contract",
    "EdgeKind",
    "ErrorContract",
    "Evidence",
    "EvidenceKind",
    "EvidenceStatus",
    "Invariant",
    "ModelValidationError",
    "NodeKind",
    "OperationContract",
    "PortDirection",
    "PortSpec",
    "ProjectSpec",
    "Requirement",
    "RequirementKind",
    "RequirementPriority",
    "RunStatus",
    "TaskKind",
    "TaskStatus",
    "UnsupportedSchemaVersion",
    "validate_release_traceability",
]
