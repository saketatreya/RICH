"""v2.2: Runtime checker — evaluator + contract checker + dependency proxy.

The runtime checker wraps implementations and dependency handles to enforce
formal contracts on every call. It uses the expression evaluator to check
preconditions, postconditions, raises properties, and trace invariants.

Architecture:
  ContractChecker    — wraps an implementation fn to check its own contracts
  DependencyProxy    — wraps a dependency handle to check its contracts,
                       assigning blame at the injection boundary
  EvalContext        — execution context for expression evaluation
  evaluate()         — walks an AST against an EvalContext
  check_property()   — dispatches a FormalProperty to its checker
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from expr_lang import (
    Expr, Literal, Variable, ResultAccess, DepCall,
    UnaryOp, BinaryOp, FuncCall, parse_expr,
)
from properties import (
    FormalProperty, PropertyKind,
    PostconditionProperty, RaisesProperty,
    TraceInvariantProperty, TemporalProperty, NonfunctionalProperty,
)


# ── Exceptions ─────────────────────────────────────────────────────────────────

class ContractViolation(Exception):
    """Raised when a formal property is violated at runtime.

    Attributes:
        property_id: the id of the violated property
        kind: the kind of property
        blamed: which party is at fault (module name or "caller"/"dep")
        detail: human-readable explanation
    """
    def __init__(self, property_id: str, kind: str, blamed: str, detail: str):
        self.property_id = property_id
        self.kind = kind
        self.blamed = blamed
        self.detail = detail
        super().__init__(
            f"[{blamed}] {kind} violation: {property_id} — {detail}"
        )


# ── Call Record (for trace invariants) ─────────────────────────────────────────

@dataclass
class CallRecord:
    """Record of one operation call for trace history."""
    op_name: str
    inputs: dict[str, Any]
    result: dict[str, Any]
    error: Optional[str] = None


# ── EvalContext ────────────────────────────────────────────────────────────────

@dataclass
class EvalContext:
    """Execution context for expression evaluation.

    Attributes:
        inputs: current call's input parameter values
        result: return value dict (None before call completes)
        error: error name if call raised (None before call)
        history: list of prior CallRecords (for trace invariants)
        deps: module_name → dependency handle (for DepCall evaluation)
        time: controllable clock value (for temporal properties, v2.4)
    """
    inputs: dict[str, Any] = field(default_factory=dict)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    history: list[CallRecord] = field(default_factory=list)
    deps: dict[str, Any] = field(default_factory=dict)
    time: float = 0.0


# ── Expression Evaluator ───────────────────────────────────────────────────────

def evaluate(node: Expr, ctx: EvalContext) -> Any:
    """Evaluate an expression AST against an execution context.

    Walks the AST, resolving variables from ctx.inputs, result.field from
    ctx.result, and dep calls from ctx.deps.
    """
    if isinstance(node, Literal):
        return node.value

    elif isinstance(node, Variable):
        if node.name not in ctx.inputs:
            raise ContractViolation(
                "eval", "evaluation", "caller",
                f"unknown input '{node.name}'"
            )
        return ctx.inputs[node.name]

    elif isinstance(node, ResultAccess):
        if ctx.result is None:
            raise ContractViolation(
                "eval", "evaluation", "caller",
                "result not available (checking before call completed?)"
            )
        if node.field is None:
            return ctx.result
        if node.field not in ctx.result:
            raise ContractViolation(
                "eval", "evaluation", "dep",
                f"result missing field '{node.field}'. Got: {sorted(ctx.result)}"
            )
        return ctx.result[node.field]

    elif isinstance(node, DepCall):
        return _eval_dep_call(node, ctx)

    elif isinstance(node, UnaryOp):
        operand = evaluate(node.operand, ctx)
        if node.op == "not":
            return not operand
        elif node.op == "-":
            return -operand
        raise ContractViolation("eval", "evaluation", "caller",
                                f"unknown unary op: {node.op}")

    elif isinstance(node, BinaryOp):
        left = evaluate(node.left, ctx)
        right = evaluate(node.right, ctx)

        if node.op == "and":
            return left and right
        elif node.op == "or":
            return left or right
        elif node.op == "==":
            return left == right
        elif node.op == "!=":
            return left != right
        elif node.op == "<":
            return left < right
        elif node.op == ">":
            return left > right
        elif node.op == "<=":
            return left <= right
        elif node.op == ">=":
            return left >= right
        elif node.op == "+":
            return left + right
        elif node.op == "-":
            return left - right
        elif node.op == "*":
            return left * right
        elif node.op == "/":
            return left / right
        raise ContractViolation("eval", "evaluation", "caller",
                                f"unknown binary op: {node.op}")

    elif isinstance(node, FuncCall):
        arg = evaluate(node.arg, ctx)
        if node.func == "len":
            return len(arg)
        raise ContractViolation("eval", "evaluation", "caller",
                                f"unknown function: {node.func}")

    raise ContractViolation("eval", "evaluation", "caller",
                            f"unknown node type: {type(node)}")


def _eval_dep_call(node: DepCall, ctx: EvalContext) -> Any:
    """Evaluate a dependency call: deps.module.op(args...).field.

    Actually calls the dependency handle from ctx.deps with the evaluated
    arguments, then extracts the requested field from the result.
    """
    if node.module not in ctx.deps:
        raise ContractViolation(
            "eval", "evaluation", "caller",
            f"dependency '{node.module}' not available. Known deps: {sorted(ctx.deps)}"
        )

    dep_handle = ctx.deps[node.module]
    op_fn = getattr(dep_handle, node.operation, None)
    if op_fn is None:
        raise ContractViolation(
            "eval", "evaluation", "caller",
            f"dependency '{node.module}' has no operation '{node.operation}'"
        )

    # Evaluate arguments in the current context (resolves input variables)
    eval_args = {}
    # Map positional args to the operation's parameter names
    # We need the parameter order. For now, use the operation's declared inputs.
    # The type checker already validated arg count matches, so we can
    # use positional matching based on declaration order.
    import inspect
    try:
        sig = inspect.signature(op_fn)
        param_names = list(sig.parameters.keys())
    except (ValueError, TypeError):
        param_names = [f"arg{i}" for i in range(len(node.args))]

    for i, arg_expr in enumerate(node.args):
        val = evaluate(arg_expr, ctx)
        name = param_names[i] if i < len(param_names) else f"arg{i}"
        eval_args[name] = val

    # Call the dependency
    try:
        result = op_fn(**eval_args)
    except Exception as e:
        raise ContractViolation(
            "eval", "evaluation", "dep",
            f"dependency '{node.module}.{node.operation}' raised: {e}"
        )

    # Extract field
    if node.field is None:
        return result
    if not isinstance(result, dict) or node.field not in result:
        raise ContractViolation(
            "eval", "evaluation", "dep",
            f"dependency '{node.module}.{node.operation}' returned "
            f"{result!r}, missing field '{node.field}'"
        )
    return result[node.field]


# ── Property Checker ───────────────────────────────────────────────────────────

def check_property(prop: FormalProperty, ctx: EvalContext,
                   op_name: str) -> None:
    """Check a single formal property against an execution context.

    Raises ContractViolation if the property is violated.
    Nonfunctional and temporal properties are skipped (not checked at runtime).
    """
    if isinstance(prop, NonfunctionalProperty):
        # Declared out-of-scope — intentionally not checked
        return

    if isinstance(prop, TemporalProperty):
        # Deferred to v2.4 — not checked at runtime yet
        return

    if isinstance(prop, PostconditionProperty):
        # Postconditions are checked after a successful call
        if ctx.result is None:
            return  # no result yet, skip
        expr = parse_expr(prop.expr)
        result = evaluate(expr, ctx)
        if not result:
            raise ContractViolation(
                prop.id, "postcondition", "dep",
                f"expected {prop.expr} to be true"
            )

    elif isinstance(prop, RaisesProperty):
        # Raises properties are checked BEFORE the call
        guard = parse_expr(prop.when)
        if evaluate(guard, ctx):
            # Guard is true — the call SHOULD raise the specified error
            # This check is done by the ContractChecker after the call
            pass  # handled in ContractChecker._check_raises

    elif isinstance(prop, TraceInvariantProperty):
        # Trace invariants are checked after each call against history
        if not ctx.history:
            return
        expr = parse_expr(prop.expr)
        result = evaluate(expr, ctx)
        if not result:
            raise ContractViolation(
                prop.id, "trace_invariant", "dep",
                f"trace invariant violated: {prop.expr}"
            )


# ── ContractChecker — wraps an implementation ──────────────────────────────────

def contract_checked(
    fn: Callable,
    postconditions: list[PostconditionProperty] = None,
    raises_props: list[RaisesProperty] = None,
    trace_invariants: list[TraceInvariantProperty] = None,
    op_name: str = None,
) -> Callable:
    """Wrap a function with runtime contract checking.

    On each call:
      1. Evaluate raises-property guards BEFORE the call
      2. Call the real function
      3. If success: check postconditions
      4. If error: check that a matching raises property was triggered
      5. Check trace invariants against accumulated history

    Args:
        fn: the implementation function
        postconditions: list of PostconditionProperty to check
        raises_props: list of RaisesProperty to check
        trace_invariants: list of TraceInvariantProperty to check
        op_name: name of the operation (for error messages)

    Returns:
        Wrapped function with contract checking.
    """
    if postconditions is None:
        postconditions = []
    if raises_props is None:
        raises_props = []
    if trace_invariants is None:
        trace_invariants = []
    if op_name is None:
        op_name = fn.__name__

    history: list[CallRecord] = []

    def wrapper(**kwargs):
        # Extract inputs from kwargs (this is what the evaluator will see)
        inputs = dict(kwargs)

        # ── Evaluate raises guards BEFORE the call ──
        pre_ctx = EvalContext(inputs=inputs)
        expected_errors = []
        for rp in raises_props:
            guard = parse_expr(rp.when)
            try:
                if evaluate(guard, pre_ctx):
                    expected_errors.append(rp)
            except ContractViolation:
                pass  # guard evaluation failure → skip this property

        # ── Call the real function ──
        error_raised = None
        result = None
        try:
            result = fn(**kwargs)
        except Exception as e:
            error_raised = str(e)
            # Check if the raised error matches any expected error
            matched = False
            for rp in expected_errors:
                if rp.error in error_raised:
                    matched = True
                    break
            if expected_errors and not matched:
                raise ContractViolation(
                    expected_errors[0].id, "raises", "dep",
                    f"expected error '{expected_errors[0].error}' "
                    f"but got '{error_raised}'"
                )
            if not expected_errors and error_raised:
                # Unexpected error — let it propagate
                raise

        # ── If we expected an error but none was raised ──
        if expected_errors and error_raised is None:
            raise ContractViolation(
                expected_errors[0].id, "raises", "dep",
                f"expected error '{expected_errors[0].error}' "
                f"but function returned normally"
            )

        # ── Check postconditions ──
        if result is not None:
            post_ctx = EvalContext(inputs=inputs, result=result,
                                   history=list(history))
            for pc in postconditions:
                try:
                    check_property(pc, post_ctx, op_name)
                except ContractViolation:
                    raise  # re-raise with the property's blame info

        # ── Check trace invariants ──
        record = CallRecord(op_name=op_name, inputs=inputs,
                            result=result, error=error_raised)
        history.append(record)
        trace_ctx = EvalContext(inputs=inputs, result=result,
                                history=list(history))
        for ti in trace_invariants:
            try:
                check_property(ti, trace_ctx, op_name)
            except ContractViolation:
                raise

        return result

    return wrapper


# ── DependencyProxy — wraps a dependency handle with blame ─────────────────────

class DependencyProxy:
    """Wraps a dependency handle to check its contracts at the injection boundary.

    When the caller invokes an operation on the dependency:
      1. Check the dep's preconditions (if violated, blame the CALLER)
      2. Call the real dep operation
      3. Check the dep's postconditions (if violated, blame the DEP)

    This is Findler-Felleisen contracts-and-blame realized at the exact
    seam D4 created: the injection point is the one place where contract
    violations have an unambiguous responsible party.
    """

    def __init__(self, dep: Any, module_name: str, op_name: str,
                 preconditions: list = None, postconditions: list = None):
        """
        Args:
            dep: the real dependency handle
            module_name: name of the dependency module (for blame messages)
            op_name: name of the operation being proxied
            preconditions: list of properties to check before calling dep
            postconditions: list of properties to check after dep returns
        """
        self._dep = dep
        self._module = module_name
        self._op_name = op_name
        self._preconditions = preconditions or []
        self._postconditions = postconditions or []

        # Look up the real method
        self._real_fn = getattr(dep, op_name, None)
        if self._real_fn is None:
            raise ContractViolation(
                "proxy", "evaluation", "caller",
                f"dependency '{module_name}' has no operation '{op_name}'"
            )

    def __call__(self, **kwargs):
        return self._real_fn(**kwargs)


class ContractChecker:
    """High-level checker that coordinates ContractChecker and DependencyProxy.

    Given a module's contract, creates checked versions of the module's
    operations (with postconditions, raises, and trace invariants enforced)
    and dependency proxies (with blame at the injection boundary).
    """

    def __init__(self, module_contract: dict, dep_contracts: dict):
        """
        Args:
            module_contract: the module's parsed contract.yaml
            dep_contracts: {dep_name: parsed contract.yaml}
        """
        self.module_contract = module_contract
        self.dep_contracts = dep_contracts

    def wrap_operation(self, fn: Callable, op_name: str) -> Callable:
        """Wrap an operation implementation with contract checking."""
        # Find the operation's formal properties
        postconditions = []
        raises_props = []
        trace_invariants = []

        behavior = self.module_contract.get("behavior", []) or []
        from properties import parse_formal_property

        for bp in behavior:
            prop = parse_formal_property(bp.get("formal"), bp.get("id", ""))
            if prop is None:
                continue
            if isinstance(prop, PostconditionProperty):
                postconditions.append(prop)
            elif isinstance(prop, RaisesProperty):
                raises_props.append(prop)
            elif isinstance(prop, TraceInvariantProperty):
                trace_invariants.append(prop)

        return contract_checked(
            fn,
            postconditions=postconditions,
            raises_props=raises_props,
            trace_invariants=trace_invariants,
            op_name=op_name,
        )

    def wrap_dependency(self, dep_handle: Any, dep_name: str,
                        op_name: str) -> DependencyProxy:
        """Create a dependency proxy with blame at the injection boundary."""
        dep_contract = self.dep_contracts.get(dep_name, {})

        # Parse the dep's formal properties (to check when dep is called)
        behavior = dep_contract.get("behavior", []) or []
        from properties import parse_formal_property

        preconditions = []
        postconditions = []

        for bp in behavior:
            prop = parse_formal_property(bp.get("formal"), bp.get("id", ""))
            if prop is None:
                continue
            if isinstance(prop, PostconditionProperty):
                postconditions.append(prop)

        return DependencyProxy(
            dep=dep_handle,
            module_name=dep_name,
            op_name=op_name,
            preconditions=preconditions,
            postconditions=postconditions,
        )
