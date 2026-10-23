"""Install the adjacent pycloudedge checkout before starting the HA image."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    source = Path("/pycloudedge")
    manifest = Path("/config/custom_components/cloudedge/manifest.json")
    requirements = json.loads(manifest.read_text())["requirements"]
    pins = [item.split("==", 1)[1] for item in requirements if item.startswith("pycloudedge==")]
    if len(pins) != 1 or not pins[0]:
        raise RuntimeError("The integration must pin exactly one pycloudedge version")
    version = pins[0]

    # Build in writable scratch space; keep virtualenvs, Git data and account
    # configuration out of the copy. The source checkout stays read-only.
    with tempfile.TemporaryDirectory(prefix="cloudedge-build-") as directory:
        build = Path(directory)
        shutil.copytree(
            source / "cloudedge", build / "cloudedge",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        for name in ("pyproject.toml", "setup.py", "README.md", "LICENSE", "requirements.txt"):
            shutil.copy2(source / name, build / name)
        environment = os.environ.copy()
        # Match HA's manifest so its dependency resolver retains this local
        # build instead of replacing it with the published package.
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", str(build)],
            env=environment,
            check=True,
        )

    print(f"CloudEdge development: installed local pycloudedge {version}", flush=True)
    command = sys.argv[1:] or ["/init"]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
