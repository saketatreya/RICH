"""The lock refresher's premises, checked without the network it needs to run.

``tools/refresh_nextjs_lock.py`` cuts the template from the largest scaffold and
rewrites one module. What can be held offline is that the largest scaffold is
what the shipped template was cut from, that the rewrite keeps the docstring,
and that the validator accepts what the pack ships.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from richbuild.target_packs import nextjs

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def refresher():
    spec = importlib.util.spec_from_file_location(
        "refresh_nextjs_lock", ROOT / "tools" / "refresh_nextjs_lock.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_template_is_the_largest_scaffolds_lock_verbatim(refresher):
    """No importer surgery applies to the scaffold the template was cut from."""

    files = refresher.largest_scaffold().render_files()
    assert files["pnpm-lock.yaml"].decode() == nextjs.PNPM_LOCK_TEMPLATE


def test_the_shipped_template_passes_the_refreshers_validation(refresher):
    refresher.validate_lock(nextjs.PNPM_LOCK_TEMPLATE)


def test_rewriting_keeps_everything_before_the_template(refresher):
    module = '"""Provenance.\n\nMore.\n"""\n\nPNPM_LOCK_TEMPLATE = """old\n"""\n'
    assert refresher.rewritten_module(module, "new\n") == (
        '"""Provenance.\n\nMore.\n"""\n\nPNPM_LOCK_TEMPLATE = """new\n"""\n'
    )
    with pytest.raises(SystemExit):
        refresher.rewritten_module("no template here\n", "new\n")


def test_a_lock_the_string_literal_cannot_carry_is_refused(refresher):
    for poisoned in (
        nextjs.PNPM_LOCK_TEMPLATE + '"""',
        nextjs.PNPM_LOCK_TEMPLATE.replace("importers:", "importers: \\"),
    ):
        with pytest.raises(SystemExit):
            refresher.validate_lock(poisoned)
