"""The guard against a test run reaching this PC's real state lives in `ns_common.testguard` (the studio and
the bare-Python setup server may not import `ns_supervisor`). This name stays for the code that imports it
from here: the same objects, nothing of its own."""

from ns_common.testguard import (
    ENV,
    TASK_SCHEDULER,
    RealMachineUnderTest,
    hook_installed,
    install,
    real_keyring,
    refuse_machine_writes,
    refuse_real,
    refuse_real_home,
    refuse_real_keyring,
    refuse_real_shortcut,
    refuse_task_scheduler,
    under_test,
)

__all__ = [
    "ENV", "TASK_SCHEDULER", "RealMachineUnderTest", "hook_installed", "install", "real_keyring",
    "refuse_machine_writes", "refuse_real", "refuse_real_home", "refuse_real_keyring", "refuse_real_shortcut",
    "refuse_task_scheduler", "under_test",
]
