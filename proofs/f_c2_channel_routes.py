"""F-C2: the channel routes.

Create (201), a bad draft (422 invalid_profile), the same slug again (409 channel_exists), GET and PUT, pause
and resume shown in /api/status, and validate naming the bad niche. The effects are read where the Channels
page reads them (the answers, /api/status and /api/channels), and where the product keeps them: the profile
file `config/channels/<slug>.yaml`, the `channels` row and the `incidents` rows of the pause and the resume.
Ported from runs/qa/croutes/croutes.py box F-C2 (evidence log 2026-11-09, 0.1.14).

The proof makes its own channel `proof-croutes` (the first free `proof-croutes-N` when an earlier run left
one; there is no delete route) and leaves it paused, so a nightly run never picks it up.
"""

from __future__ import annotations

import re
from typing import Any

from proofs._lib import db_rows, short

PROOF = {
    "id": "f-c2-channel-routes",
    "boxes": ["F-C2"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 300,
    "fakes": [],
}

BASE = "proof-croutes"
PAUSE_NOTE = "proof F-C2: pause"
RESUME_NOTE = "proof F-C2: resume"
FINAL_NOTE = "proof F-C2: left paused"


def _body(slug: str, niche: str) -> dict[str, Any]:
    """The profile the Channels page sends for a new channel (its blank object) with an id, name and niche."""
    return {
        "slug": slug,
        "display_name": "Proof " + slug,
        "status": "active",
        "language": "en",
        "niche": niche,
        "seed_keywords": ["proof croutes", "route check"],
        "voice_notes": "Plain and calm.",
        "voice": {"mode": "preset", "speaker": "Ryan", "description": None, "name": None, "speed": 1},
        "format": "master_first",
        "masters_per_week": 1,
        "long_minutes": [10, 14],
        "shorts_per_master": [4, 6],
        "shorts_per_day_max": 2,
        "length_buckets": [35, 50, 75],
        "publish_window": "18:00-21:00",
        "audience": "general",
        "made_for_kids": False,
        "fiction_label": True,
        "creator_note": "",
        "critic_bar": 8,
        "qc_pass_target": 0.9,
        "pools": {
            "hooks": ["cold_open", "question", "confession"],
            "structures": ["twist", "reverse_reveal", "slow_burn"],
            "pov_tones": ["first_person_intimate", "narrator_calm", "dry_wit"],
            "pacings": ["standard", "measured", "brisk"],
            "caption_styles": ["punch_yellow", "clean_white", "soft_shadow"],
            "visual_treatments": ["dark_slowzoom", "handheld_grit", "still_life"],
            "title_formulas": ["question", "statement_twist", "i_never"],
        },
        "avoid_topics": [],
        "formats_allowed": [],
        "captions": {"mask_profanity": False},
        "created_at": None,
    }


def _error(answer: Any) -> dict[str, Any]:
    """The `error` object of a refusal, `{}` for anything else."""
    err = answer.get("error") if isinstance(answer, dict) else None
    return err if isinstance(err, dict) else {}


def _status_of(ctx: Any, slug: str) -> str | None:
    """The channel's status as /api/status (the header and the Overview read it) shows it."""
    code, st = ctx.api("GET", "/api/status")
    ctx.check(code == 200 and isinstance(st, dict), "GET /api/status answers", status=code)
    return next((c.get("status") for c in st.get("channels") or [] if c.get("slug") == slug), None)


def _card_of(ctx: Any, slug: str) -> dict[str, Any] | None:
    """The channel's card in GET /api/channels (the Channels page)."""
    code, cards = ctx.api("GET", "/api/channels")
    ctx.check(code == 200 and isinstance(cards, list), "GET /api/channels answers a list", status=code)
    return next((c for c in cards if c.get("slug") == slug), None)


def _free_slug(ctx: Any) -> str:
    code, cards = ctx.api("GET", "/api/channels")
    ctx.check(code == 200 and isinstance(cards, list), "GET /api/channels lists the channels", status=code)
    taken = {c.get("slug") for c in cards}
    slug = BASE
    n = 1
    while slug in taken:
        n += 1
        slug = f"{BASE}-{n}"
    ctx.step(
        "the proof's own channel (no delete route, so it stays, paused)",
        channel=slug,
        taken=sorted(str(t) for t in taken if str(t).startswith("proof-")),
    )
    return slug


def run(ctx: Any) -> None:
    slug = _free_slug(ctx)
    niche = f"proof quiet lighthouse mysteries {slug}"
    body = _body(slug, niche)
    yaml_file = ctx.home / "config" / "channels" / f"{slug}.yaml"

    # ---- validate: a good draft, then a bad one that must name the niche
    code, good = ctx.api("POST", f"/api/channels/{slug}/validate", body)
    ctx.check(
        code == 200 and good.get("ok") is True and good.get("errors") == [],
        "validate(good draft for a channel that does not exist yet) answers ok=true with no errors",
        status=code,
        answer=short(good),
    )
    bad = {k: v for k, v in body.items() if k != "niche"}
    bad.update({"publish_window": "9-5", "critic_bar": 1, "masters_per_week": 9})
    code, vb = ctx.api("POST", f"/api/channels/{slug}/validate", bad)
    fields = {e.get("field"): e.get("message") for e in (vb.get("error_fields") or [])}
    ctx.check(
        code == 200 and vb.get("ok") is False and "niche" in fields,
        "validate(bad draft) answers ok=false and names the niche among the fields",
        status=code,
        fields=fields,
        errors=vb.get("errors"),
    )
    ctx.check(
        {"publish_window", "critic_bar", "masters_per_week"} <= set(fields)
        and len(vb.get("errors") or []) == len(fields),
        "validate(bad draft) also names the window, the critic bar and the cadence, one plain message each",
        fields=fields,
    )

    # ---- create: refused with a bad draft, nothing written
    bad_slug = f"{slug}-bad"
    code, refused = ctx.api("POST", "/api/channels", {**bad, "slug": bad_slug})
    err = _error(refused)
    detail_fields = [d.get("field") for d in err.get("details") or []]
    ctx.check(
        code == 422 and err.get("code") == "invalid_profile" and "niche" in detail_fields,
        "POST /api/channels with the bad draft answers 422 invalid_profile naming the niche",
        status=code,
        error_code=err.get("code"),
        detail_fields=detail_fields,
    )
    code, _ = ctx.api("GET", f"/api/channels/{bad_slug}")
    ctx.check(
        code == 404
        and not (ctx.home / "config" / "channels" / f"{bad_slug}.yaml").exists()
        and db_rows(ctx, "select count(*) from channels where slug=?", (bad_slug,)) == [(0,)],
        "the refused draft left no profile file and no channels row",
        status=code,
    )

    # ---- create: 201, then the effects
    ctx.check(_card_of(ctx, slug) is None, "before the create the channel is not in the list", channel=slug)
    code, made = ctx.api("POST", "/api/channels", body)
    profile = made.get("profile") or {}
    ctx.check(
        code == 201 and profile.get("slug") == slug and profile.get("status") == "active",
        "POST /api/channels answers 201 with the saved profile",
        status=code,
        slug=profile.get("slug"),
        profile_status=profile.get("status"),
        created_at=profile.get("created_at"),
    )
    rows = db_rows(ctx, "select display_name, status from channels where slug=?", (slug,))
    ctx.check(
        yaml_file.is_file() and rows == [("Proof " + slug, "active")],
        "the profile file config/channels/<slug>.yaml and the channels row exist",
        yaml_file=str(yaml_file.relative_to(ctx.home)),
        rows=rows,
    )
    card = _card_of(ctx, slug)
    ctx.check(
        card is not None and card.get("niche") == niche and card.get("status") == "active",
        "the channel is in GET /api/channels (the Channels page) with its niche, active",
        card=short(card),
    )
    code, again = ctx.api("POST", "/api/channels", body)
    err = _error(again)
    ctx.check(
        code == 409 and err.get("code") == "channel_exists",
        "POST /api/channels with the same slug answers 409 channel_exists",
        status=code,
        error_code=err.get("code"),
        message=err.get("message"),
    )

    # ---- GET and PUT
    code, got = ctx.api("GET", f"/api/channels/{slug}")
    gp = got.get("profile") or {}
    ctx.check(
        code == 200
        and gp.get("niche") == niche
        and gp.get("publish_window") == "18:00-21:00"
        and gp.get("seed_keywords") == body["seed_keywords"]
        and "formats_in_use" in got,
        "GET /api/channels/{slug} answers the profile as saved (and the formats in use)",
        status=code,
        niche=gp.get("niche"),
        keys=sorted(got),
    )
    edited = niche + " (edited)"
    code, put = ctx.api("PUT", f"/api/channels/{slug}", {**gp, "niche": edited})
    ctx.check(
        code == 200 and (put.get("profile") or {}).get("niche") == edited,
        "PUT /api/channels/{slug} stores the new niche",
        status=code,
        answer=short(put),
    )
    code, got = ctx.api("GET", f"/api/channels/{slug}")
    on_disk = yaml_file.read_text(encoding="utf-8")
    card = _card_of(ctx, slug)
    ctx.check(
        (got.get("profile") or {}).get("niche") == edited
        and edited in on_disk
        and (card or {}).get("niche") == edited,
        "the edit shows in GET, in the profile file and on the Channels page card",
        card_niche=(card or {}).get("niche"),
    )
    code, moved = ctx.api("PUT", f"/api/channels/{slug}", {**gp, "slug": slug + "-moved", "niche": edited})
    err = _error(moved)
    ctx.check(
        code == 422
        and err.get("code") == "invalid_profile"
        and not (ctx.home / "config" / "channels" / f"{slug}-moved.yaml").exists(),
        "PUT with another slug is refused 422 (the channel id cannot be changed) and writes no file",
        status=code,
        message=err.get("message"),
    )

    # ---- pause and resume, shown in /api/status
    ctx.check(_status_of(ctx, slug) == "active", "/api/status shows the channel active before the pause")
    code, paused = ctx.api("POST", f"/api/channels/{slug}/pause", {"note": PAUSE_NOTE})
    shown = _status_of(ctx, slug)
    rows = db_rows(ctx, "select status from channels where slug=?", (slug,))
    ctx.check(
        code == 200
        and (paused.get("profile") or {}).get("status") == "paused"
        and shown == "paused"
        and rows == [("paused",)]
        and re.search(r"^status: paused$", yaml_file.read_text("utf-8"), re.M),
        "pause answers paused and /api/status, the channels row and the profile file show paused",
        status=code,
        status_shown=shown,
        rows=rows,
    )
    code, resumed = ctx.api("POST", f"/api/channels/{slug}/resume", {"note": RESUME_NOTE})
    shown = _status_of(ctx, slug)
    rows = db_rows(ctx, "select status from channels where slug=?", (slug,))
    ctx.check(
        code == 200
        and (resumed.get("profile") or {}).get("status") == "active"
        and shown == "active"
        and rows == [("active",)]
        and re.search(r"^status: active$", yaml_file.read_text("utf-8"), re.M),
        "resume answers active and /api/status, the channels row and the profile file show active",
        status=code,
        status_shown=shown,
        rows=rows,
    )
    code, final = ctx.api("POST", f"/api/channels/{slug}/pause", {"note": FINAL_NOTE})
    ctx.check(
        code == 200 and _status_of(ctx, slug) == "paused",
        "the proof leaves its channel paused so a nightly run does not use it",
        status=code,
    )
    incidents = db_rows(ctx, "select kind, note from incidents where channel=? order by incident_id", (slug,))
    ctx.check(
        incidents == [("pause", PAUSE_NOTE), ("resume", RESUME_NOTE), ("pause", FINAL_NOTE)],
        "the pause, the resume and the last pause each wrote an incidents row with their note",
        incidents=incidents,
    )

    # ---- validate on the saved profile, and the unknown channel
    code, saved = ctx.api("POST", f"/api/channels/{slug}/validate")
    ctx.check(
        code == 200 and saved.get("ok") is True,
        "validate with no body checks the saved profile and answers ok=true",
        status=code,
        warnings=saved.get("warnings"),
    )
    look_alike = {**body, "slug": f"{slug}-twin", "niche": edited}
    code, twin = ctx.api("POST", f"/api/channels/{slug}-twin/validate", look_alike)
    fields = [w.get("field") for w in twin.get("warning_fields") or []]
    ctx.check(
        code == 200 and "niche" in fields,
        "validate(a draft with this channel's niche and language) warns that the niche is taken",
        status=code,
        warnings=twin.get("warnings"),
    )
    for verb, path in (("GET", ""), ("POST", "/pause"), ("POST", "/resume")):
        url = f"/api/channels/proof-ghost-channel{path}"
        code, ghost = ctx.api(verb, url, {} if verb == "POST" else None)
        err = _error(ghost)
        ctx.check(
            code == 404 and err.get("code") == "channel_not_found",
            f"{verb} /api/channels/<unknown>{path} answers 404 channel_not_found",
            status=code,
            error_code=err.get("code"),
        )
