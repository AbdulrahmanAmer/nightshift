"""docs/FINALIZATION.md is the box list of what is left; every box has an id in its section and a proof, and a
ticked box carries evidence (dev/finalization_status.py --check)."""

from __future__ import annotations

from dev import finalization_status as fs


def test_the_list_parses_and_every_ticked_box_has_evidence():
    items, problems = fs.parse(fs.DOC.read_text(encoding="utf-8"))
    assert problems == []
    assert {i.section for i in items} == set("ABCDEFG")
    assert len(items) >= 60 and any(i.done for i in items) and any(not i.done for i in items)


def test_the_check_refuses_a_ticked_box_without_evidence_and_a_misplaced_id():
    text = (
        "## A. One\n- [x] F-A1 done but silent (proof).\n- [ ] F-B1 wrong section (proof).\n"
        "- [ ] F-A2 fine (proof).\n"
    )
    items, problems = fs.parse(text)
    assert [i.id for i in items] == ["F-A1", "F-B1", "F-A2"]
    assert any("without `evidence:`" in p for p in problems)
    assert any("sits under section A" in p for p in problems)
    assert "A" in fs.table(items)
