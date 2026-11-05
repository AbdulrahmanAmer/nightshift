"""The verification inventory (docs/VERIFICATION_INVENTORY.md) lists every checkable item the code defines."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    script = ROOT / "dev" / "verification_inventory.py"
    spec = importlib.util.spec_from_file_location("verification_inventory", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_the_inventory_covers_every_item_the_code_defines():
    vi = _module()
    items = vi.collect()
    known = vi.parse_inventory(vi.INVENTORY.read_text(encoding="utf-8"))
    missing = [f"{k}: {i}" for k, i in items if (k, i) not in known]
    hint = "run `python dev/verification_inventory.py` and give these a status: "
    assert not missing, hint + ", ".join(missing)
    stale = [f"{k}: {i}" for k, i in known if (k, i) not in items]
    assert not stale, "no longer in the code, remove from the inventory: " + ", ".join(stale)
    bad = [f"{k}: {i} = {s}" for (k, i), (s, _e) in known.items() if s not in vi.STATUSES]
    assert not bad, bad


def test_the_inventory_is_what_the_generator_writes():
    vi = _module()
    msg = "docs/VERIFICATION_INVENTORY.md is out of date: run dev/verification_inventory.py"
    assert vi.main(["--check"]) == 0, msg


def test_the_inventory_spans_every_block():
    vi = _module()
    items = vi.collect()
    kinds = {k for k, _i in items}
    expected = {"api", "cli", "stage", "installer-step", "launcher", "page", "assistant-tool", "service",
                "workflow"}
    assert kinds >= expected
    assert sum(1 for k, _i in items if k == "api") >= 80
    assert sum(1 for k, _i in items if k == "assistant-tool") >= 60
