"""What an installed wheel serves, and what a checkout serves."""

from pathlib import Path

from richbuild.api import canvas_origin, default_web_root


def _canvas(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "index.html").write_text("<div id=\"root\"></div>")
    return root


def test_a_checkout_serves_its_own_build_over_the_bundled_copy(tmp_path):
    bundled = _canvas(tmp_path / "site-packages" / "richbuild" / "canvas")
    repo = _canvas(tmp_path / "checkout" / "web" / "dist")
    assert canvas_origin(bundled=bundled, repo_dist=repo) == ("repo", repo)


def test_an_installed_wheel_serves_the_bundled_canvas(tmp_path):
    bundled = _canvas(tmp_path / "site-packages" / "richbuild" / "canvas")
    repo = tmp_path / "nowhere" / "web" / "dist"
    assert canvas_origin(bundled=bundled, repo_dist=repo) == ("bundled", bundled)


def test_nothing_built_names_where_a_checkout_would_build(tmp_path):
    bundled = tmp_path / "site-packages" / "richbuild" / "canvas"
    (tmp_path / "checkout" / "web").mkdir(parents=True)
    (tmp_path / "checkout" / "pyproject.toml").write_text("[project]\n")
    repo = tmp_path / "checkout" / "web" / "dist"
    assert canvas_origin(bundled=bundled, repo_dist=repo) == ("missing", repo)


def test_nothing_built_in_an_installed_wheel_names_the_package(tmp_path):
    # site-packages/../.. exists (it is the venv), but it is no checkout: the
    # remedy must not send an operator to build a web/ that is not there.
    bundled = tmp_path / "venv" / "lib" / "site-packages" / "richbuild" / "canvas"
    repo = tmp_path / "venv" / "lib" / "web" / "dist"
    (tmp_path / "venv" / "lib").mkdir(parents=True)
    assert canvas_origin(bundled=bundled, repo_dist=repo) == ("missing", bundled)


def test_default_web_root_is_the_origin_the_process_would_serve():
    origin, path = canvas_origin()
    assert default_web_root() == path
    assert origin in {"repo", "bundled", "missing"}


def test_no_checkout_metadata_disagrees_with_pyproject():
    """`rich --version` and `/v1/health.version` read the installed
    distribution's metadata, and on a checkout that comes from whatever
    build-time metadata directory is on the path. This host carried two: a
    current one under `src/` and a stale `rich_agent_build_system.egg-info`
    at the root, left by the pre-`src/` layout, declaring 1.0.0 for a tree
    whose pyproject said 2.0.0.dev0. Which one answered depended on sys.path
    order, so `python -m richbuild.cli --version` reported 1.0.0 while the
    suite saw 2.0.0.dev0 -- checking only what this process resolves would
    have missed it. Check every one of them instead."""

    import tomllib

    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "version"
    ]
    found: dict[Path, str] = {}
    for pattern in ("*.egg-info", "*.dist-info"):
        for directory in (*root.glob(pattern), *(root / "src").glob(pattern)):
            for name in ("PKG-INFO", "METADATA"):
                metadata = directory / name
                if not metadata.exists():
                    continue
                for line in metadata.read_text(errors="replace").splitlines():
                    if line.startswith("Version:"):
                        found[directory] = line.split(":", 1)[1].strip()
                        break
                break

    stale = {
        directory: version
        for directory, version in found.items()
        if version != declared
    }
    assert not stale, (
        "distribution metadata in the checkout disagrees with pyproject "
        f"({declared}): "
        + ", ".join(
            f"{d.relative_to(root)} says {v}" for d, v in sorted(stale.items())
        )
        + ". Delete the stale directory; it is a build artifact, and whichever "
        "one sys.path finds first is the version the CLI reports."
    )
