"""F-C2: the channel routes.

Create (201), a bad draft (422 invalid_profile), the same slug again (409 channel_exists), GET and PUT, pause
and resume shown in /api/status, and validate naming the bad niche. The effects are read where the Channels
page reads them (the answers, /api/status and /api/channels), and where the product keeps them: the profile
file `config/channels/<slug>.yaml`, the `channels` row and the `incidents` rows of the pause and the resume.
Ported from runs/qa/croutes/croutes.py box F-C2 (evidence log 2026-11-09, 0.1.14).

The channel is created on the Channels page itself (Playwright): "Add a channel", the name, short id and
subject typed, "Save channel". The proof reads the POST /api/channels request the page sent and checks its
body is the page's blank profile (`BLANK` in Channels.tsx) plus the three typed fields, then that the page
lists the channel. The route checks (201, 422, 409, GET and PUT, pause and resume, validate) go through
`ctx.api`.

The proof makes its own channel `proof-croutes` (the first free `proof-croutes-N` when an earlier run left
one: there is no delete route, and a create needs a slug that does not exist). Whatever happens, the run ends
with a cleanup that pauses it through a call with no deadline, so a nightly run never picks it up.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from proofs._lib import api_no_deadline, db_rows, short

PROOF = {
    "id": "f-c2-channel-routes",
    "boxes": ["F-C2"],
    "install": "dev",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 300,
    "fakes": [],
}

BASE = "proof-croutes"
PAUSE_NOTE = "proof F-C2: pause"
RESUME_NOTE = "proof F-C2: resume"
FINAL_NOTE = "proof F-C2: left paused"


# The Channels page's blank profile (BLANK in ns_dashboard/ui/src/pages/Channels.tsx, line 90). The page sends
# it with the name, the short id and the subject the owner typed.
BLANK: dict[str, Any] = {
    "slug": "",
    "display_name": "",
    "status": "active",
    "language": "en",
    "niche": "",
    "seed_keywords": [],
    "voice_notes": "",
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
        "pov_tones": ["first_person_intimate", "narrator_calm"],
        "pacings": ["standard", "measured"],
        "caption_styles": ["punch_yellow", "clean_white"],
        "visual_treatments": ["dark_slowzoom", "handheld_grit"],
        "title_formulas": ["question", "statement_twist", "i_never"],
    },
    "avoid_topics": [],
    "formats_allowed": [],
    "captions": {"mask_profanity": False},
    "created_at": None,
}


def _typed(slug: str, niche: str) -> dict[str, Any]:
    """What the proof types into the page: the three fields the owner fills in."""
    return {"slug": slug, "display_name": "Proof " + slug, "niche": niche}


def _expected_body(slug: str, niche: str) -> dict[str, Any]:
    """The body the page must send: its blank profile with the typed fields."""
    return {**json.loads(json.dumps(BLANK)), **_typed(slug, niche)}


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


def _is_create(method: str, url: str) -> bool:
    return method == "POST" and urlparse(url).path == "/api/channels"


def _create_on_page(ctx: Any, slug: str, niche: str) -> dict[str, Any]:
    """Create the channel on the Channels page; returns the body of the POST /api/channels the page sent."""
    typed = _typed(slug, niche)
    page = ctx.page()
    sent: list[dict[str, Any]] = []
    answered: list[int] = []
    page.on(
        "request",
        lambda r: sent.append({"url": r.url, "body": r.post_data}) if _is_create(r.method, r.url) else None,
    )
    page.on(
        "response",
        lambda r: answered.append(r.status) if _is_create(r.request.method, r.url) else None,
    )
    page.goto(f"http://127.0.0.1:{ctx.ports['dashboard']}/#/channels", wait_until="load")
    page.get_by_role("button", name="Add a channel").first.click(timeout=30000)
    page.locator("#f-name").fill(typed["display_name"], timeout=30000)
    page.locator("#f-slug").fill(typed["slug"])  # after the name: the page derives an id from the name
    page.locator("#f-niche").fill(typed["niche"])
    ctx.step("typed into the new-channel form", **typed)
    page.get_by_role("button", name="Save channel").first.click(timeout=30000)
    dialogs: list[str] = []

    def posted() -> bool:
        if sent and answered:
            return True
        # the page asks before saving when validate has a warning; "Save anyway" is its own button
        again = page.get_by_role("button", name="Save anyway")
        if again.count() and again.first.is_visible():
            dialogs.append("Save anyway")
            again.first.click()
        stuck = page.get_by_role("button", name="Back to editing")
        if stuck.count() and stuck.first.is_visible():
            ctx.fail("the Channels page refused the new channel: " + page.inner_text("body")[:300])
        return False

    ctx.wait(posted, 60, every=1, what="the Channels page to send POST /api/channels")
    body = json.loads(sent[0]["body"] or "null")
    want = _expected_body(slug, niche)
    differs = sorted(k for k in set(body) | set(want) if body.get(k) != want.get(k))
    ctx.check(
        len(sent) == 1 and body == want,
        "the page sent POST /api/channels with its blank profile plus the typed name, short id and subject",
        sent_body=body,
        differs=differs,
        typed=typed,
        dialogs=dialogs,
    )
    ctx.check(answered == [201], "the page's POST /api/channels was answered 201", statuses=answered)
    ctx.wait(
        lambda: typed["display_name"] in page.inner_text("body"),
        30,
        every=1,
        what="the Channels page to list the new channel",
    )
    text = page.inner_text("body")
    ctx.check(
        typed["display_name"] in text,
        "the Channels page lists the new channel",
        display_name=typed["display_name"],
        page_text=text[:300].replace("\n", " / "),
    )
    page.screenshot(path=str(ctx.scratch / "channels-after-create.png"), full_page=True)
    return body


def _pause_leftover(ctx: Any, slug: str) -> None:
    """Cleanup, pass or fail: the proof's channel ends paused. Calls with no deadline (ctx.api refuses once
    the proof's time is used up), and the result is recorded in a step."""
    try:
        status, got = api_no_deadline(ctx, "GET", f"/api/channels/{slug}")
        profile = (got.get("profile") or {}) if isinstance(got, dict) else {}
        if status != 200:
            ctx.step("cleanup: proof channel not found, nothing to pause", channel=slug, status=status)
            return
        if profile.get("status") == "paused":
            ctx.step("cleanup: proof channel is paused", channel=slug)
            return
        status, answer = api_no_deadline(
            ctx, "POST", f"/api/channels/{slug}/pause", {"note": "proof F-C2: paused by the cleanup"}
        )
        ctx.step("cleanup: proof channel paused", channel=slug, status=status, answer=short(answer))
    except Exception as exc:  # noqa: BLE001  (a cleanup error must not hide the proof's own result)
        ctx.step("cleanup: pausing the proof channel failed", channel=slug, error=str(exc))


def run(ctx: Any) -> None:
    slug = _free_slug(ctx)
    try:
        _prove(ctx, slug)
    finally:
        _pause_leftover(ctx, slug)


def _prove(ctx: Any, slug: str) -> None:
    niche = f"proof quiet lighthouse mysteries {slug}"
    body = _expected_body(slug, niche)
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
    _create_on_page(ctx, slug, niche)
    code, made = ctx.api("GET", f"/api/channels/{slug}")
    profile = made.get("profile") or {}
    ctx.check(
        code == 200 and profile.get("slug") == slug and profile.get("status") == "active",
        "the channel the page created is saved: GET answers its profile, active",
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
