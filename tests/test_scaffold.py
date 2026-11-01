import subprocess
import sys


def test_version_importable():
    import ns_common

    assert ns_common.__version__ == "0.1.0"


def test_cli_stubs_run():
    for mod in ("ns_studio.cli", "ns_supervisor.cli"):
        out = subprocess.run([sys.executable, "-m", mod], capture_output=True, text=True, check=True)
        assert "0.1.0" in out.stdout
