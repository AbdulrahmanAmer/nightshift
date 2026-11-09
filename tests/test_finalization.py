"""docs/FINALIZATION.md is the box list of what is left; every box has an id in its section and a proof, and a
ticked box carries evidence (dev/finalization_status.py --check)."""

from __future__ import annotations

from dev import finalization_status as fs


def test_the_list_parses_and_every_ticked_box_has_evidence():
    items, problems = fs.parse_all()
    assert problems == []
    assert {i.section for i in items} == set("ABCDEFGH")      # H: docs/FEATURE_STATUS.md feature gaps
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


def test_a_tick_may_cite_a_proof_id_and_the_check_verifies_the_citation():
    proofs = {"f-b7-review": ["F-B7"]}
    head = "## B. One\n"
    cited = head + "- [x] F-B7 done. evidence: proof:f-b7-review (runs/proofs/0.1.15/F-B7.json)\n"
    assert fs.parse(cited, proofs)[1] == []
    unknown = head + "- [x] F-B7 done. evidence: proof:f-b7-nothing\n"
    assert any("no proofs/*.py declares it" in p for p in fs.parse(unknown, proofs)[1])
    other_box = head + "- [x] F-B9 done. evidence: proof:f-b7-review\n"
    assert any("does not declare box F-B9" in p for p in fs.parse(other_box, proofs)[1])
    open_box = head + "- [ ] F-B7 open, a note says proof:f-b7-nothing\n"
    assert fs.parse(open_box, proofs)[1] == []  # only a tick is held to its citation
    assert fs.parse(unknown)[1] == []  # without the table of proofs nothing is checked
    plain = head + "- [x] F-B7 done. evidence: test_x and the proof for it (not an id)\n"
    assert fs.parse(plain, proofs)[1] == []


def test_the_check_knows_the_proofs_in_the_repo():
    known = fs.known_proofs()
    assert known["f-b18-studio-status"] == ["F-B18"] and known["f-c1-status-events"] == ["F-C1"]
