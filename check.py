"""check.py — Contract verification harness.

Instantiates each module, wraps its dependencies with DependencyProxy
(contract-checked at the injection boundary), and exercises every operation.
Reports violations with blame.

Usage:
    python3 check.py --all     # Run all modules with checked deps
    python3 check.py auth      # Run just one module
"""

import sys
import os
import importlib.util
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime_checker import (
    DependencyProxy, contract_checked, EvalContext,
    ContractViolation, check_property,
)
from properties import (
    parse_formal_property, PostconditionProperty,
    RaisesProperty, TraceInvariantProperty,
)
from expr_lang import parse_expr


# ── Known-good test inputs ────────────────────────────────────────────────────

KNOWN_INPUTS = {
    "auth": {
        "authenticate": {"username": "admin", "password": "secret"},
    },
    "token_store": {
        "issue": {"subject": "alice"},
        "validate": {"token": None},  # filled dynamically
    },
    "user_repo": {
        "verify_password": {"username": "admin", "password": "secret"},
    },
}


def get_good_inputs(module_name, op_name):
    return KNOWN_INPUTS.get(module_name, {}).get(op_name, {})


def load_module_code(module_name):
    """Load a module's source as a Python module."""
    src_file = f"modules/{module_name}/src/{module_name}.py"
    if not os.path.isfile(src_file):
        return None
    spec = importlib.util.spec_from_file_location(
        f"modules.{module_name}", src_file
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_contract(name):
    with open(f"modules/{name}/contract.yaml") as f:
        return yaml.safe_load(f)


def find_class_and_instance(mod, module_name):
    """Find the implementation class in a module and instantiate it."""
    target = module_name.replace("_", "")
    best = None

    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if not isinstance(attr, type) or attr_name.startswith("_"):
            continue
        if attr_name.endswith("Error"):
            continue
        # Prefer the class whose name matches the module name
        if attr_name.lower().replace("_", "") == target:
            best = attr
            break
        if best is None:
            best = attr

    if best is not None:
        try:
            return best()
        except TypeError:
            # Can't instantiate (e.g. dataclass with required fields)
            # Try next candidate
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type) and not attr_name.startswith("_") and \
                   attr is not best and not attr_name.endswith("Error"):
                    try:
                        return attr()
                    except TypeError:
                        continue

    return None


def find_operation(instance, mod, op_name):
    """Find an operation — method on instance, or module-level function."""
    # Try instance method first
    fn = getattr(instance, op_name, None)
    if fn is not None and callable(fn):
        return fn
    # Fall back to module-level function
    fn = getattr(mod, op_name, None)
    if fn is not None and callable(fn):
        return fn
    return None


def check_module(module_name, verbose=True):
    """Instantiate module, wrap deps with proxies, exercise ops. Returns (passed, violations)."""
    contract = load_contract(module_name)
    deps = contract.get("dependencies", []) or []
    mod = load_module_code(module_name)
    if mod is None:
        return False, [f"Could not load '{module_name}'"]

    instance = find_class_and_instance(mod, module_name)
    # instance can be None for module-level functions (e.g. auth)

    violations = []

    # ── Build checked dependency proxies ──
    checked_deps = {}
    for dep_name in deps:
        dep_contract = load_contract(dep_name)
        dep_mod = load_module_code(dep_name)
        if dep_mod is None:
            violations.append(f"Could not load dependency '{dep_name}'")
            continue
        dep_instance = find_class_and_instance(dep_mod, dep_name)
        if dep_instance is None:
            violations.append(f"No class found in '{dep_name}'")
            continue
        proxy = DependencyProxy(dep_instance, dep_name, dep_contract=dep_contract)
        checked_deps[dep_name] = proxy

    # ── Parse formal properties ──
    behavior = contract.get("behavior", []) or []
    postconditions = []
    raises_props = []
    trace_invariants = []

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

    # ── Exercise each operation ──
    interface = contract.get("interface", {}) or {}
    history = []  # accumulated across all ops in this module

    for op_spec in interface.get("operations", []) or []:
        op_name = op_spec.get("name", "")
        op_fn = find_operation(instance, mod, op_name)
        if op_fn is None:
            violations.append(f"Operation '{op_name}' not found on {module_name}")
            continue

        # Build inputs: prefer known-good, fall back to synthetic
        inputs = get_good_inputs(module_name, op_name).copy()
        if not inputs:
            for pname, ptype in op_spec.get("inputs", {}).items():
                if ptype == "string":
                    inputs[pname] = "test_value"
                elif ptype == "int":
                    inputs[pname] = 42
                elif ptype == "bool":
                    inputs[pname] = True
                else:
                    inputs[pname] = "test"

        # Sanitize dynamic inputs (e.g. validate needs a real token)
        if module_name == "token_store" and op_name == "validate":
            if not inputs.get("token"):
                # Issue first, then validate
                issue_fn = getattr(instance, "issue", None)
                if issue_fn:
                    r = issue_fn(subject="alice")
                    inputs["token"] = r["token"]

        # ── Call the operation ──
        try:
            # Check if it's a module-level function that needs dep injection
            import inspect
            sig = inspect.signature(op_fn)
            # If the function has keyword-only params (after *), those are deps
            has_kw_only = any(
                p.kind == inspect.Parameter.KEYWORD_ONLY
                for p in sig.parameters.values()
            )
            if has_kw_only:
                result = op_fn(**inputs, **checked_deps)
            else:
                result = op_fn(**inputs)

            if verbose:
                result_str = str(result)[:60]
                print(f"  ✓ {module_name}.{op_name}({', '.join(f'{k}={v}' for k,v in inputs.items())}) → {result_str}")

            # Postconditions
            if result is not None and postconditions:
                ctx = EvalContext(inputs=inputs, result=result, deps=checked_deps)
                for pc in postconditions:
                    try:
                        check_property(pc, ctx, op_name)
                    except ContractViolation as e:
                        violations.append(str(e))
                        if verbose:
                            print(f"    ❌ {e}")

            # Trace invariants — only accumulated for THIS operation
            from dataclasses import dataclass
            @dataclass
            class SimpleRecord:
                inputs: dict
                result: dict
                error: str = None
                op_name: str = None

            history.append(SimpleRecord(inputs=inputs, result=result, op_name=op_name))
            op_history = [r for r in history if r.op_name == op_name]
            if trace_invariants:
                trace_ctx = EvalContext(inputs=inputs, result=result,
                                        history=list(op_history), deps=checked_deps)
                for ti in trace_invariants:
                    try:
                        check_property(ti, trace_ctx, op_name)
                    except ContractViolation as e:
                        violations.append(str(e))
                        if verbose:
                            print(f"    ❌ trace: {e}")

        except Exception as e:
            if verbose:
                print(f"  ⚡ {module_name}.{op_name}(...) raised: {e}")

            error_str = str(e)
            declared = False
            for rp in raises_props:
                for err_name in rp.errors:
                    if err_name in error_str:
                        declared = True
                        break
            if raises_props and not declared:
                all_errors = []
                for rp in raises_props:
                    all_errors.extend(rp.errors)
                violations.append(
                    f"[{module_name}] raised '{error_str[:40]}' "
                    f"not in declared errors: {all_errors}"
                )
                if verbose:
                    print(f"    ❌ undeclared error: {error_str[:50]}")

    return len(violations) == 0, violations


def check_all():
    """Run contract checking on all modules."""
    print("╔" + "═" * 60 + "╗")
    print("║  CONTRACT VERIFICATION" + " " * 38 + "║")
    print("╚" + "═" * 60 + "╝")
    print()

    all_passed = True
    for name in sorted(os.listdir("modules")):
        mod_dir = f"modules/{name}"
        if not os.path.isdir(mod_dir):
            continue
        if not os.path.isfile(f"{mod_dir}/contract.yaml"):
            continue

        print(f"─── {name} ───")
        passed, violations = check_module(name, verbose=True)
        if not passed:
            all_passed = False
            for v in violations:
                print(f"  ❌ {v}")
        print()

    print("═" * 60)
    if all_passed:
        print("  ALL MODULES PASS CONTRACT CHECKS ✅")
    else:
        print("  SOME CONTRACT CHECKS FAILED ❌")
    print("═" * 60)
    return 0 if all_passed else 1


if __name__ == "__main__":
    if "--all" in sys.argv:
        sys.exit(check_all())
    elif len(sys.argv) > 1:
        module = sys.argv[1]
        passed, violations = check_module(module)
        if not passed:
            for v in violations:
                print(f"  ❌ {v}")
        sys.exit(0 if passed else 1)
    else:
        print("Usage: python check.py <module> | --all")
        sys.exit(1)
