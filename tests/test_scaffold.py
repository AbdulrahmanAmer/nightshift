import subprocess
import sys
from pathlib import Path


def test_version_importable():
    import ns_common

    assert ns_common.__version__ == (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()


def test_cli_stubs_run():
    for mod in ("ns_studio.cli", "ns_supervisor.cli"):
        out = subprocess.run([sys.executable, "-m", mod], capture_output=True, text=True, check=True)
        assert __import__("ns_common").__version__ in out.stdout
