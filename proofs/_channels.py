"""Shared by the worked proofs (H-13): which channel profiles the product can load on this home."""


def loadable_channels(ctx):
    """(profiles on disk, profiles the product reports as UNREADABLE, the loadable ones), all sorted.

    The product skips a profile that cannot be loaded and keeps the others (`studio channel list` names it
    as UNREADABLE), so a proof must not expect that one in `studio status` or in the dashboard."""
    channels_dir = ctx.home / "config" / "channels"
    on_disk = sorted(p.stem for p in channels_dir.glob("*.yaml")) if channels_dir.is_dir() else []
    listing = ctx.cli(["studio", "channel", "list"], 120)
    unreadable = sorted(
        parts[0]
        for parts in (line.split("\t") for line in (listing.stdout or "").splitlines())
        if len(parts) > 1 and parts[1] == "UNREADABLE"
    )
    return on_disk, unreadable, [s for s in on_disk if s not in unreadable]
