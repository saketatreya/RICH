"""Keep target technologies out of the core model layer.

``models.py`` is the vocabulary every target pack is written against. Once a
pack's technology leaks into it, the next pack has to either adopt that
technology's assumptions or work around them, and the seam stops being a seam.

The rule is enforced mechanically rather than by review because the existing
violation -- a browser locator vocabulary in core, dating from when Next.js was
the only pack -- is exactly what a reviewer stops noticing. The exemption set
below is frozen: it may shrink, never grow.
"""

import ast
import re
from pathlib import Path

import pytest

import rich_v2.models as models


MODELS_PATH = Path(models.__file__)
CORE_MODULES = ("models.py", "budget.py", "store.py")

# Names that only make sense once a specific target technology is chosen.
BANNED_WORDS = frozenset(
    {
        "aria",
        "browser",
        "chromium",
        "elan",
        "eslint",
        "fastcheck",
        "jest",
        "lake",
        "lean",
        "mathlib",
        "neon",
        "nextjs",
        "olean",
        "playwright",
        "pnpm",
        "psycopg",
        "react",
        "sorry",
        "theorem",
        "tsc",
        "typescript",
        "vercel",
        "vitest",
        "webpack",
    }
)

# The browser-in-core debt that predates the target-pack seam. Frozen: a new
# identifier carrying one of these words is a regression even though the word
# itself appears here, because the entry names the symbol, not the word.
LEGACY_IDENTIFIERS = frozenset(
    {
        "BrowserLocator",
        "BrowserLocatorKind",
        "_ARIA_ROLES",
    }
)
# String literals are prose -- mostly error messages derived from the symbols
# above -- so they are held to the looser word-level ceiling. A leaked command
# name like "pnpm" still trips it.
LEGACY_LITERAL_WORDS = frozenset({"aria", "browser"})


def _identifier_words(name: str) -> set[str]:
    """Split an identifier into lowercase words on case and separator boundaries."""

    parts = re.split(r"[^A-Za-z0-9]+", name)
    words: set[str] = set()
    for part in parts:
        for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part):
            words.add(word.lower())
    return words


def _declared_identifiers(tree: ast.AST) -> set[str]:
    """Every name this module writes down: symbols, attributes, imports, arguments."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _flagged(names: set[str]) -> dict[str, str]:
    """Map each offending identifier to the banned word it carries."""

    offenders: dict[str, str] = {}
    for name in names:
        hits = _identifier_words(name) & BANNED_WORDS
        if hits:
            offenders[name] = sorted(hits)[0]
    return offenders


def test_the_word_splitter_does_not_manufacture_false_positives():
    # "boolean" ends in "lean" and "invariant" contains "aria"; a substring
    # scan would flag both and the guard would be turned off within a week.
    assert _identifier_words("boolean") == {"boolean"}
    assert _identifier_words("Invariant") == {"invariant"}
    assert _identifier_words("ValueTypeKind") == {"value", "type", "kind"}
    assert _identifier_words("_ARIA_ROLES") == {"aria", "roles"}
    assert _identifier_words("browser_locator") == {"browser", "locator"}
    assert not _identifier_words("boolean") & BANNED_WORDS
    assert not _identifier_words("Invariant") & BANNED_WORDS


def test_models_names_no_target_technology_beyond_the_frozen_legacy_set():
    tree = ast.parse(MODELS_PATH.read_text())
    offenders = _flagged(_declared_identifiers(tree))

    new = set(offenders) - LEGACY_IDENTIFIERS
    assert not new, (
        "core models must not name a target technology; new offenders: "
        f"{sorted((name, offenders[name]) for name in new)}"
    )


def test_the_legacy_exemption_set_is_a_ceiling_not_a_wishlist():
    # Every exempted name must still be present. When the browser vocabulary
    # finally moves behind the pack seam, this fails and the entry is deleted
    # -- which is how the ceiling ratchets down instead of drifting.
    tree = ast.parse(MODELS_PATH.read_text())
    offenders = _flagged(_declared_identifiers(tree))

    stale = LEGACY_IDENTIFIERS - set(offenders)
    assert not stale, f"exemptions no longer needed, delete them: {sorted(stale)}"


def _string_literal_words(tree: ast.AST) -> set[str]:
    """Words appearing in string literals, excluding docstrings."""

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    words: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            words |= _identifier_words(node.value)
    return words


def test_models_string_literals_name_no_new_target_technology():
    tree = ast.parse(MODELS_PATH.read_text())

    offending = _string_literal_words(tree) & BANNED_WORDS

    assert offending <= LEGACY_LITERAL_WORDS, (
        f"core model literals must not name a target technology: {sorted(offending)}"
    )


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_modules_import_nothing_from_a_target_pack(module):
    tree = ast.parse((MODELS_PATH.parent / module).read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            if node.level:
                imported = "." * node.level + imported
            assert "target_packs" not in imported, (
                f"{module} imports {imported!r}; the dependency runs the other way"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "target_packs" not in alias.name


def test_models_depends_only_on_the_standard_library():
    tree = ast.parse(MODELS_PATH.read_text())
    relative = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]

    assert not relative, (
        "models.py is the bottom of the v2 layering and must import no sibling "
        f"module; found {[node.module for node in relative]}"
    )


def test_the_obligation_vocabulary_itself_stays_technology_free():
    # The point of the whole stage: these names must read the same to a
    # property-test compiler and to a proof compiler.
    vocabulary = {
        member.value
        for enum in (models.ObligationRelation, models.ObligationTier, models.ValueTypeKind)
        for member in enum
    }

    for term in vocabulary:
        assert not _identifier_words(term) & BANNED_WORDS, term
