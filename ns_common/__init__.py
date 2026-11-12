__version__ = "0.1.15-rc8"

from ns_common import testguard as _testguard  # noqa: E402 - the version line stays first

# A process a test starts (`ns`, `studio`, the wizard, the setup server) gets the audit hook that keeps it
# away from this PC's real state, whatever its entry point. In a normal process this call does nothing.
_testguard.install()
