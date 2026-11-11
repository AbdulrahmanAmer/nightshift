"""The release keys this version of Nightshift trusts (card update-signing). Data only; standard library only.

An update package installs only when `<zip>.sig` verifies with one of these keys (`ns_common.sigcheck`,
`ns_supervisor.update.verify_package`). `TRUSTED` is the ONLY trust anchor of the program
(`ns_supervisor.releases.trusted_keys` returns it and nothing else): no environment variable, flag, dev mode,
test release source or file of the home adds or swaps a key. A key is `{key id: base64 of the 32-byte
Ed25519 public key}`, the id being the first 16 hex digits of the key's SHA-256.

`TRUSTED` is empty until the owner has made the release key once (`dev/release_keys.ps1 -Create`, OPERATOR.md
"The release key"): that script writes the primary and the backup key between the two marker lines. While it
is empty this version accepts a package on its SHA256 alone and says so. A later version may add or drop a key
here for the versions after it: a package is checked against the list of the program that is INSTALLED.

No test key is listed here and none is kept in the repository: a test makes a throwaway key and puts its
public half in `TRUSTED` of its own process (a patch), and a proof installs a program built with a throwaway
key pinned between the markers (`dev/release_keys.ps1 -DryRun -KeysFile <copy>` writes exactly that).
"""

from __future__ import annotations

# BEGIN TRUSTED (dev/release_keys.ps1 -Create replaces the lines down to END TRUSTED)
TRUSTED: dict[str, str] = {}
# END TRUSTED
