"""v2.0: Property schema and classifier.

Widens the formal field from Optional[str] to a structured object
with a `kind` discriminator. Each kind has its own required fields
and validation rules.

Kinds:
  postcondition  — checked after each call, predicate over result
  raises         — checked on error path, predicate over when + error
  trace_invariant — checked across call history, predicate over trace
  temporal       — cross-call temporal logic, checked with clock control
  nonfunctional  — declared out-of-scope for contract checking (e.g. timing)
"""

from enum import Enum
from typing import Optional


class PropertyKind(str, Enum):
    """Discriminator for formal property types."""
    PRECONDITION = "precondition"
    POSTCONDITION = "postcondition"
    RAISES = "raises"
    TRACE_INVARIANT = "trace_invariant"
    TEMPORAL = "temporal"
    NONFUNCTIONAL = "nonfunctional"


class PropertyParseError(Exception):
    """Raised when a formal property specification is malformed."""
    pass


class FormalProperty:
    """Base class for all formal property types."""

    def __init__(self, kind: PropertyKind, id: str):
        self.kind = kind
        self.id = id

    def __repr__(self):
        return f"{self.__class__.__name__}(id={self.id!r}, kind={self.kind.value})"


class PreconditionProperty(FormalProperty):
    """A predicate checked before each call.

    expr: expression over inputs, evaluated before the call.
    If violated, blame the CALLER (at the injection boundary).
    """

    def __init__(self, id: str, expr: str):
        super().__init__(PropertyKind.PRECONDITION, id)
        self.expr = expr

    def __repr__(self):
        return f"PreconditionProperty(id={self.id!r}, expr={self.expr!r})"


class PostconditionProperty(FormalProperty):
    """A predicate checked after each successful call.

    expr: expression over inputs and result, evaluated after the call returns.
    """

    def __init__(self, id: str, expr: str):
        super().__init__(PropertyKind.POSTCONDITION, id)
        self.expr = expr

    def __repr__(self):
        return f"PostconditionProperty(id={self.id!r}, expr={self.expr!r})"


class RaisesProperty(FormalProperty):
    """Declared error behavior — observable, not predictive.

    errors: list of error names this operation may raise.
    when: optional pure predicate over INPUTS ONLY (no deps calls).
          If true and the function returns normally, it's a violation.
          DESIGN RULE: when must be a pure predicate over inputs.
          Deps calls in guards are unsound (re-derives the implementation's
          decision from the spec — Rice: you can observe, not predict).

    On exception:
      - If the error name is in errors → re-raise (contract satisfied)
      - If it's not → ContractViolation
    On normal return with when=true → ContractViolation
    """

    def __init__(self, id: str, errors: list[str] = None,
                 when: str = None):
        super().__init__(PropertyKind.RAISES, id)
        self.errors = errors or []
        self.when = when  # None or pure-inputs predicate

    @property
    def error(self):
        """Backward compat: first error name."""
        return self.errors[0] if self.errors else ""

    def __repr__(self):
        return f"RaisesProperty(id={self.id!r}, errors={self.errors!r}, when={self.when!r})"


class TraceInvariantProperty(FormalProperty):
    """A predicate over the history of calls to an operation.

    expr: predicate over the call trace (history), checked after each call.
    """

    def __init__(self, id: str, expr: str):
        super().__init__(PropertyKind.TRACE_INVARIANT, id)
        self.expr = expr

    def __repr__(self):
        return f"TraceInvariantProperty(id={self.id!r}, expr={self.expr!r})"


class TemporalProperty(FormalProperty):
    """A temporal logic property relating calls across time.

    expr: temporal formula (e.g. G(issue(t) → within(86400, validate(t)))).
    Checked with controllable clock and state machine model.
    """

    def __init__(self, id: str, expr: str):
        super().__init__(PropertyKind.TEMPORAL, id)
        self.expr = expr

    def __repr__(self):
        return f"TemporalProperty(id={self.id!r}, expr={self.expr!r})"


class NonfunctionalProperty(FormalProperty):
    """A property declared out-of-scope for contract-based checking.

    Examples: constant-time comparison, cache hit rates, latency percentiles.
    These cannot be verified from I/O predicates — they require different tooling
    (statistical analysis, dataflow analysis, profiling).
    """

    def __init__(self, id: str):
        super().__init__(PropertyKind.NONFUNCTIONAL, id)

    def __repr__(self):
        return f"NonfunctionalProperty(id={self.id!r})"


# ── Parser ─────────────────────────────────────────────────────────────────────

_VALID_KINDS = {k.value for k in PropertyKind}

_REQUIRED_FIELDS = {
    PropertyKind.PRECONDITION: {"expr"},
    PropertyKind.POSTCONDITION: {"expr"},
    PropertyKind.RAISES: {"errors"},
    PropertyKind.TRACE_INVARIANT: {"expr"},
    PropertyKind.TEMPORAL: {"expr"},
    PropertyKind.NONFUNCTIONAL: set(),
}


def parse_formal_property(raw, property_id: str) -> Optional[FormalProperty]:
    """Parse a formal property from a contract's behavior entry.

    Args:
        raw: The `formal` field from a behavior property dict, or None.
        property_id: The stable id from the behavior property.

    Returns:
        A FormalProperty subclass, or None if raw is None (backward compat).

    Raises:
        PropertyParseError: if the kind is unknown or required fields are missing.
    """
    if raw is None:
        return None

    if not isinstance(raw, dict):
        raise PropertyParseError(
            f"[{property_id}] formal must be a dict or null, got {type(raw).__name__}"
        )

    kind_str = raw.get("kind")
    if kind_str is None:
        raise PropertyParseError(
            f"[{property_id}] formal dict requires a 'kind' field"
        )

    if kind_str not in _VALID_KINDS:
        raise PropertyParseError(
            f"[{property_id}] unknown formal kind '{kind_str}'. "
            f"Valid kinds: {sorted(_VALID_KINDS)}"
        )

    kind = PropertyKind(kind_str)
    required = _REQUIRED_FIELDS[kind]

    missing = required - set(raw.keys())
    if missing:
        raise PropertyParseError(
            f"[{property_id}] kind '{kind_str}' requires fields: {sorted(missing)}"
        )

    if kind == PropertyKind.PRECONDITION:
        return PreconditionProperty(id=property_id, expr=raw["expr"])
    elif kind == PropertyKind.POSTCONDITION:
        return PostconditionProperty(id=property_id, expr=raw["expr"])
    elif kind == PropertyKind.RAISES:
        return RaisesProperty(
            id=property_id,
            errors=raw.get("errors", []),
            when=raw.get("when"),  # optional pure-inputs guard
        )
    elif kind == PropertyKind.TRACE_INVARIANT:
        return TraceInvariantProperty(id=property_id, expr=raw["expr"])
    elif kind == PropertyKind.TEMPORAL:
        return TemporalProperty(id=property_id, expr=raw["expr"])
    elif kind == PropertyKind.NONFUNCTIONAL:
        return NonfunctionalProperty(id=property_id)

    raise PropertyParseError(f"[{property_id}] unhandled kind: {kind_str}")
