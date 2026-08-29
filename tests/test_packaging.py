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
    repo = tmp_path / "checkout" / "web" / "dist"
    assert canvas_origin(bundled=bundled, repo_dist=repo) == ("missing", repo)


def test_default_web_root_is_the_origin_the_process_would_serve():
    origin, path = canvas_origin()
    assert default_web_root() == path
    assert origin in {"repo", "bundled", "missing"}
