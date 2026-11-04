"""The installer's live key check must not look like Python-urllib: Cloudflare in front of Pixabay and
Pexels answers 403 (error 1010) to that signature, which made every valid key look rejected (v0.1.5)."""

from installer.stage2.web import keys


def _capture(status: int):
    seen = []

    def fetch(req):
        seen.append(req)
        return status

    return seen, fetch


def test_both_services_send_the_product_user_agent():
    for service in ("pexels", "pixabay"):
        seen, fetch = _capture(200)
        assert keys.check_key(service, "42657536-0a2a3e4ddcea18f37caaeeb9f", fetch=fetch)["ok"]
        ua = seen[0].get_header("User-agent")
        assert ua and ua.startswith("Nightshift-Setup/"), (service, ua)
        assert "urllib" not in ua.lower()


def test_pexels_still_sends_the_key_as_authorization():
    seen, fetch = _capture(200)
    keys.check_key("pexels", "abc", fetch=fetch)
    assert seen[0].get_header("Authorization") == "abc"
    assert "Authorization" not in str(seen[0].full_url)


def test_pixabay_sends_the_key_in_the_query_only():
    seen, fetch = _capture(200)
    keys.check_key("pixabay", "abc", fetch=fetch)
    assert "key=abc" in seen[0].full_url
    assert seen[0].get_header("Authorization") is None


def test_a_403_is_still_reported_as_a_rejected_key():
    _, fetch = _capture(403)
    assert keys.check_key("pixabay", "abc", fetch=fetch)["code"] == "NS-E502"
