"""The release keys this version of Nightshift trusts (card update-signing). Data only; standard library only.

An update package installs only when `<zip>.sig` verifies with one of these keys (`ns_common.sigcheck`,
`ns_supervisor.update.verify_package`); which keys count on this install is decided in one place,
`ns_supervisor.releases.trusted_keys`. A key is `{key id: base64 of the 32-byte Ed25519 public key}`, the id
being the first 16 hex digits of the key's SHA-256.

`TRUSTED` is empty until the owner has made the release key once (`dev/release_keys.ps1 -Create`, OPERATOR.md
"The release key"): that script writes the primary and the backup key between the two marker lines. While it
is empty this version accepts a package on its SHA256 alone and says so. A later version may add or drop a key
here for the versions after it: a package is checked against the list of the program that is INSTALLED.

`TEST_KEYS` is the key of tests and proofs. Its private half is public
(`tests/fixtures/release_test_key.txt`), so it counts only on a dev or engine-test install that reads its
releases from a test release source (`NS_RELEASE_SOURCE`), never on any other install.
"""

from __future__ import annotations

# BEGIN TRUSTED (dev/release_keys.ps1 -Create replaces the lines down to END TRUSTED)
TRUSTED: dict[str, str] = {}
# END TRUSTED

TEST_KEYS: dict[str, str] = {"10a38f1ee1f7a4b7": "R4xCaZTWs1HjkmN6dQ7Y+J/SbthjUKFcuJjFnOC/ViU="}
