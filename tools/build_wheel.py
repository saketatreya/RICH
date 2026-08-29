"""Build the wheel an operator installs: the package with the canvas inside.

`pip wheel .` alone would ship a server with nothing to serve.  This builds
the canvas (`npm ci` when web/node_modules is missing, then `npm run build`),
copies web/dist into src/richbuild/canvas so setuptools packages it as data,
assembles the wheel into dist/, and removes the copy again so a checkout keeps
serving its own fresh build (`default_web_root` prefers web/dist when the
repository is present).  Every step is a real command with its exit code
honoured; nothing here is skipped quietly.

    python tools/build_wheel.py                 # → dist/rich_agent_build_system-*.whl
    python tools/build_wheel.py --keep          # leave src/richbuild/canvas in place
    python tools/build_wheel.py --skip-install  # web/node_modules is already there (CI)
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DIST = WEB / "dist"
CANVAS = ROOT / "src" / "richbuild" / "canvas"
WHEELS = ROOT / "dist"


def run(argv: list[str], **kwargs) -> None:
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, check=True, cwd=ROOT, **kwargs)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--keep", action="store_true", help="keep src/richbuild/canvas after building")
    parser.add_argument(
        "--skip-install", action="store_true", help="web/node_modules is already installed (CI)"
    )
    args = parser.parse_args(argv[1:])

    # The canvas is always built here: a wheel assembled from a stale or
    # absent web/dist would ship the wrong product without a word.
    if not args.skip_install and not (WEB / "node_modules").is_dir():
        run(["npm", "--prefix", str(WEB), "ci"])
    run(["npm", "--prefix", str(WEB), "run", "build"])
    if not (DIST / "index.html").is_file():
        print(f"no canvas at {DIST}; build it first", file=sys.stderr)
        return 1

    if CANVAS.exists():
        shutil.rmtree(CANVAS)
    shutil.copytree(DIST, CANVAS)
    print(f"+ copied {DIST} -> {CANVAS}")
    # setuptools reuses build/lib across builds, so a module deleted since the
    # last build, or an older canvas, would ship silently. Start clean.
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    try:
        WHEELS.mkdir(exist_ok=True)
        run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(WHEELS)])
    finally:
        if not args.keep and CANVAS.exists():
            shutil.rmtree(CANVAS)
            print(f"+ removed {CANVAS}")
    wheels = sorted(WHEELS.glob("rich_agent_build_system-*.whl"))
    if not wheels:
        print("no wheel was produced", file=sys.stderr)
        return 1
    print(f"wheel: {wheels[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
