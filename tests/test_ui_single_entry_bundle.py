"""The two UI builds ship ONE script, `app.js` (F-6, from Review's note on PR 154).

The servers name the entry with a version token (`./app.js?v=<token>`, D-29). A Rollup chunk imports the
entry as `../app.js`, without the query, and the browser treats the two URLs as two modules: the whole
bundle would load and run twice (two React roots) as soon as the build holds a second chunk. The fix is in
the build: no code splitting (`inlineDynamicImports`), so there is never a chunk to import the entry.
These tests read the committed output (`ns_dashboard/static`, `installer/ui/static`; the CI job `ui-build`
proves it equals a fresh build) and the two Vite configs that make it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDS = {
    "dashboard": ROOT / "ns_dashboard" / "static",
    "installer": ROOT / "installer" / "ui" / "static",
}
CONFIGS = {
    "dashboard": ROOT / "ns_dashboard" / "ui" / "vite.config.ts",
    "installer": ROOT / "ns_dashboard" / "ui" / "vite.installer.config.ts",
}
#: A static `from"./x.js"` or a dynamic `import("./x.js")` of another script file inside a built script.
RELATIVE_SCRIPT_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)["'](\.{1,2}/[^"']*\.js)(?:\?[^"']*)?["']"""
)


@pytest.mark.parametrize("name", sorted(BUILDS))
def test_build_output_holds_exactly_one_script(name: str) -> None:
    root = BUILDS[name]
    scripts = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.js"))
    assert scripts == ["app.js"], (
        f"{name} build holds more than the entry (a lazy chunk imports ../app.js): {scripts}"
    )


@pytest.mark.parametrize("name", sorted(BUILDS))
def test_the_entry_imports_no_other_script(name: str) -> None:
    text = (BUILDS[name] / "app.js").read_text(encoding="utf-8")
    assert RELATIVE_SCRIPT_IMPORT.findall(text) == [], f"{name} app.js loads another script file"


@pytest.mark.parametrize("name", sorted(BUILDS))
def test_the_page_loads_only_the_entry_script(name: str) -> None:
    html = (BUILDS[name] / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r"""<script\b[^>]*\bsrc="([^"]+)\"""", html)
    preloads = re.findall(r"""<link\b[^>]*\brel="modulepreload"[^>]*\bhref="([^"]+)\"""", html)
    assert sources == ["./app.js"], sources
    assert preloads == [], preloads


def _inlines_dynamic_imports(config_text: str) -> bool:
    """True when `inlineDynamicImports: true` is live code; comments are cut out first."""
    code = re.sub(r"/\*.*?\*/", "", config_text, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    return re.search(r"inlineDynamicImports\s*:\s*true", code) is not None


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_vite_config_forbids_code_splitting(name: str) -> None:
    """A future `lazy(() => import(...))` must fold into `app.js`, not become a chunk that imports it."""
    text = CONFIGS[name].read_text(encoding="utf-8")
    assert _inlines_dynamic_imports(text), f"{CONFIGS[name].name} allows a second chunk"


def test_a_commented_out_inline_option_does_not_count() -> None:
    """The config check reads code, not comments (Break-it round, F-6)."""
    assert _inlines_dynamic_imports("output: {\n  inlineDynamicImports: true,\n}")
    assert not _inlines_dynamic_imports("output: {\n  // inlineDynamicImports: true,\n}")
    assert not _inlines_dynamic_imports("output: {\n  /* inlineDynamicImports: true, */\n}")
    assert not _inlines_dynamic_imports(
        "output: {\n  inlineDynamicImports: false, // inlineDynamicImports: true\n}"
    )


def test_the_checks_catch_a_chunk_that_imports_the_entry(tmp_path: Path) -> None:
    """The guard itself: the pattern Rollup writes for a lazy chunk is found (the old installer had one)."""
    chunk = tmp_path / "assets" / "fakeApi-BQf6Cg_N.js"
    chunk.parent.mkdir()
    chunk.write_text('import{c as fe}from"../app.js";export const x=fe;', encoding="utf-8")
    assert RELATIVE_SCRIPT_IMPORT.findall(chunk.read_text(encoding="utf-8")) == ["../app.js"]
    entry = 'const m=await import("./assets/fakeApi-BQf6Cg_N.js");'
    assert RELATIVE_SCRIPT_IMPORT.findall(entry) == ["./assets/fakeApi-BQf6Cg_N.js"]
