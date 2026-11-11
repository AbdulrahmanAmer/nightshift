"""CLI worked proof (H-13): `studio status` on an install prints what the database and the channel files say.

Box F-B18 (docs/FINALIZATION.md), its `studio status answers` half only: the launcher consoles
(`nightshift-studio.ps1`, `nightshift-code.ps1`) are NOT exercised here; the scope step says so in the
evidence. The effect is the command's own text, read back and compared with the files and rows behind it.
"""

from proofs._channels import loadable_channels

PROOF = {
    "id": "f-b18-studio-status",
    "boxes": ["F-B18"],
    "install": "any",  # it only reads (`studio status`, read-only queries): the GPU kit runs it on a real PC
    "window": False,
    "needs": [],
    "timeout_s": 180,
    "fakes": [],
}


def run(ctx):
    ctx.step(
        "scope",
        covers="`studio status` answers on this home",
        not_covered="nightshift-studio.ps1 / nightshift-code.ps1 launcher consoles",
    )
    on_disk, unreadable, slugs = loadable_channels(ctx)
    ctx.step("channel files on disk", channels=on_disk, unreadable_profiles=unreadable)

    done = ctx.cli(["studio", "status"], 120)
    out = done.stdout or ""
    ctx.check(
        done.returncode == 0, "studio status exits 0", exit_code=done.returncode, stderr=done.stderr[-500:]
    )

    if not slugs:
        ctx.check("no channels configured" in out, "an install without channels says so", stdout=out[-500:])
        return
    lines = out.splitlines()
    for slug in slugs:
        head = next((i for i, line in enumerate(lines) if line.startswith(f"channel {slug} [")), None)
        ctx.check(head is not None, f"channel {slug} has its status block", stdout=out[-500:])
        assert head is not None  # ctx.check has raised otherwise; this narrows the type
        rows = ctx.db("select status, count(*) as n from outputs where channel = ? group by status", (slug,))
        wanted = ", ".join(f"{r['status']}={r['n']}" for r in sorted(rows, key=lambda r: r["status"]))
        block = lines[head + 1 :]
        printed = next((line.strip() for line in block if line.strip().startswith("outputs:")), "")
        expected = f"outputs: {wanted or 'no outputs yet'}"
        ctx.check(
            printed == expected,
            f"channel {slug}: the printed outputs line equals the outputs table",
            printed=printed,
            expected=expected,
        )
