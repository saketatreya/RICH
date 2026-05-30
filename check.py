"""check.py — Contract verification harness.

Runs a module against its dependencies with contract-checked proxies
at the injection boundary. Reports violations with blame.

Usage:
    python check.py auth    # Run auth module with checked deps
    python check.py --all   # Run all modules with checked deps
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


def load_module_code(module_name):
    """Load a module's source as a Python module."""
    mod_dir = os.path.join("modules", module_name)
    src_file = os.path.join(mod_dir, "src", f"{module_name}.py")
    if not os.path.isfile(src_file):
        return None

    spec = importlib.util.spec_from_file_location(
        f"modules.{module_name}", src_file
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_contract(name):
    with open(f"modules/{name}/contract.yaml") as f:
        return yaml.safe_load(f)


def check_module(module_name, verbose=True):
    """Run a module against contract-checked dependencies. Returns (passed, violations)."""
    contract = load_contract(module_name)
    deps = contract.get("dependencies", []) or []

    # Load the module
    mod = load_module_code(module_name)
    if mod is None:
        return False, [f"Could not load module '{module_name}'"]

    violations = []

    # Build checked dependency proxies
    checked_deps = {}
    for dep_name in deps:
        dep_contract = load_contract(dep_name)
        dep_mod = load_module_code(dep_name)
        if dep_mod is None:
            violations.append(f"Could not load dependency '{dep_name}'")
            continue

        # Find the dep's implementation class
        # Convention: class named CamelCase(mod_name)
        dep_class = None
        for attr_name in dir(dep_mod):
            attr = getattr(dep_mod, attr_name)
            if isinstance(attr, type) and attr_name.lower().replace("_", "") == dep_name.replace("_", ""):
                dep_class = attr
                break
        if dep_class is None:
            # Fallback: any class in the module
            for attr_name in dir(dep_mod):
                attr = getattr(dep_mod, attr_name)
                if isinstance(attr, type) and not attr_name.startswith("_"):
                    if attr_name.endswith("Error"):
                        continue
                    dep_class = attr
                    break

        if dep_class is None:
            violations.append(f"No implementation class found in '{dep_name}'")
            continue

        # Create instance and wrap with proxy
        dep_instance = dep_class()
        proxy = DependencyProxy(dep_instance, dep_name, dep_contract=dep_contract)
        checked_deps[dep_name] = proxy

    # Find the module's operations and exercise them
    interface = contract.get("interface", {}) or {}
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

    # Exercise each operation
    for op_spec in interface.get("operations", []) or []:
        op_name = op_spec.get("name", "")
        op_fn = getattr(mod, op_name, None)
        if op_fn is None:
            violations.append(f"Operation '{op_name}' not found in '{module_name}'")
            continue

        # Get example inputs
        inputs = {}
        for pname, ptype in op_spec.get("inputs", {}).items():
            if ptype == "string":
                inputs[pname] = "test_value"
            elif ptype == "int":
                inputs[pname] = 42
            elif ptype == "float":
                inputs[pname] = 3.14
            elif ptype == "bool":
                inputs[pname] = True
            else:
                inputs[pname] = "test"

        # Call the operation with checked dependencies injected
        try:
            result = op_fn(**inputs, **checked_deps)
            if verbose:
                result_str = str(result)[:60]
                print(f"  ✓ {module_name}.{op_name}({', '.join(f'{k}={v}' for k,v in inputs.items())}) → {result_str}")

            # Check postconditions
            if result is not None and postconditions:
                ctx = EvalContext(inputs=inputs, result=result,
                                  deps=checked_deps)
                for pc in postconditions:
                    try:
                        check_property(pc, ctx, op_name)
                    except ContractViolation as e:
                        violations.append(str(e))
                        if verbose:
                            print(f"    ❌ {e}")

        except Exception as e:
            if verbose:
                print(f"  ⚡ {module_name}.{op_name}(...) raised: {e}")

            # Check if the error is in declared raises
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
                    f"which is not in declared errors: {all_errors}"
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
        mod_dir = os.path.join("modules", name)
        if not os.path.isdir(mod_dir):
            continue
        contract_file = os.path.join(mod_dir, "contract.yaml")
        if not os.path.isfile(contract_file):
            continue

        print(f"─── {name} ───")
        passed, violations = check_module(name, verbose=True)
        if not passed:
            all_passed = False
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
