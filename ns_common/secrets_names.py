"""Names of the secrets Nightshift keeps in the Windows Credential Manager (one place, every component).

The installer and the dashboard Settings page store the stock-footage API keys under these names; the studio
reads them back. They were once spelled differently on each side, so the studio ran with empty keys.
"""

from __future__ import annotations

#: Credential Manager service name.
KEYRING_SERVICE = "Nightshift"
PEXELS_KEY = "pexels_api_key"
PIXABAY_KEY = "pixabay_api_key"
KEY_NAMES = (PEXELS_KEY, PIXABAY_KEY)
#: Short names the studio passes around (`Services` / `build_services(keys=...)`) for each credential.
SHORT_NAMES = {PEXELS_KEY: "pexels", PIXABAY_KEY: "pixabay"}
