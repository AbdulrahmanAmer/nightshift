"""Windows integration through PowerShell (component C1): user environment variable, desktop, shortcuts.

This is the only module that names the home environment variable literally. Scripts are passed with
``-EncodedCommand`` so no quoting can go wrong.
"""

from __future__ import annotations

import base64
from pathlib import Path

from installer.stage2.runner import Runner, RunResult

HOME_VAR = "NIGHTSHIFT_HOME"


def ps_quote(value: str) -> str:
    """Escape a value for a single-quoted PowerShell string."""
    return value.replace("'", "''")


def run_ps(
    runner: Runner, script: str, *, tag: str, meta: dict[str, str] | None = None, timeout: float = 60
) -> RunResult:
    """Run a PowerShell snippet (no profile, no prompts)."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return runner.run(argv, tag=tag, meta=meta, timeout=timeout)


def set_user_env(runner: Runner, name: str, value: str) -> bool:
    """Set a user-level environment variable. True on success."""
    script = f"[Environment]::SetEnvironmentVariable('{ps_quote(name)}', '{ps_quote(value)}', 'User')"
    return run_ps(runner, script, tag="ps:set-env", meta={"name": name, "value": value}).ok


def utf8_b64_script(expression: str) -> str:
    """PowerShell that prints ``expression`` as base64 of its UTF-8 bytes.

    Windows PowerShell 5.1 writes redirected output in the console's OEM code page (437, 850, 720...), which
    turns an accented or Arabic path into ``?`` or a byte pair that is not UTF-8. Base64 is plain ASCII, so
    it survives every code page.
    """
    return (
        f"$t = {expression}\n"
        "if ($null -eq $t) { $t = '' }\n"
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$t))"
    )


def decode_utf8_b64(text: str) -> str:
    """Inverse of :func:`utf8_b64_script` ("" when the output is empty or not valid base64)."""
    raw = "".join(text.split())
    if not raw:
        return ""
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def eval_ps_text(
    runner: Runner, expression: str, *, tag: str, meta: dict[str, str] | None = None, timeout: float = 60
) -> str | None:
    """Evaluate a PowerShell expression and return its string value exactly (None when the script failed)."""
    res = run_ps(runner, utf8_b64_script(expression), tag=tag, meta=meta, timeout=timeout)
    return decode_utf8_b64(res.stdout) if res.ok else None


def get_user_env(runner: Runner, name: str) -> str | None:
    """Read a user-level environment variable (None when unset)."""
    expr = f"[Environment]::GetEnvironmentVariable('{ps_quote(name)}', 'User')"
    value = eval_ps_text(runner, expr, tag="ps:get-env", meta={"name": name})
    return value or None


def desktop_dir(runner: Runner) -> Path | None:
    """The current user's Desktop folder (follows OneDrive redirection); exact for non-ASCII paths."""
    text = eval_ps_text(runner, "[Environment]::GetFolderPath('Desktop')", tag="ps:desktop")
    return Path(text) if text else None


def create_shortcut(
    runner: Runner, lnk: Path, target: str, arguments: str, workdir: Path, description: str,
    icon: Path | None = None,
) -> bool:
    """Create a .lnk with WScript.Shell (`icon`, an .ico, becomes its IconLocation). True on success."""
    icon_part = f"$s.IconLocation = '{ps_quote(str(icon))},0'; " if icon is not None else ""
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{ps_quote(str(lnk))}'); "
        f"$s.TargetPath = '{ps_quote(target)}'; "
        f"$s.Arguments = '{ps_quote(arguments)}'; "
        f"$s.WorkingDirectory = '{ps_quote(str(workdir))}'; "
        f"$s.Description = '{ps_quote(description)}'; "
        f"{icon_part}"
        "$s.Save()"
    )
    meta = {"lnk": str(lnk), "target": target, "arguments": arguments}
    if icon is not None:
        meta["icon"] = str(icon)
    return run_ps(runner, script, tag="ps:shortcut", meta=meta).ok


# ---- toast identity (AppUserModelID) --------------------------------------------------------------------
# Windows shows a toast only for an AppUserModelID it knows. Two parts, the first one is what matters:
#   1. the registry key HKCU\Software\Classes\AppUserModelId\<id> with DisplayName and IconUri (this alone
#      makes ``CreateToastNotifier(id)`` toasts appear, which is how BurntToast does it);
#   2. System.AppUserModel.ID on the Desktop shortcut, written through the shell's IPropertyStore, so the
#      toast and the taskbar button belong to the same app. Best effort: when the COM call fails the
#      registry key still stands.
AUMID_KEY_PARENT = "HKCU:" + chr(92) + "Software" + chr(92) + "Classes" + chr(92) + "AppUserModelId"

_PROPERTY_STORE_CSHARP = """
using System;
using System.Runtime.InteropServices;

public static class NsAumid
{
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant
    {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr ptr;
        [FieldOffset(16)] public IntPtr pad;   // PROPVARIANT is 24 bytes on x64; GetValue writes all of them
    }

    [ComImport]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint cProps);
        [PreserveSig] int GetAt(uint iProp, out PropertyKey pkey);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant pv);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant pv);
        [PreserveSig] int Commit();
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(
        string path, IntPtr bindContext, int flags, ref Guid riid, out IPropertyStore store);

    [DllImport("ole32.dll")]
    static extern int PropVariantClear(ref PropVariant pv);

    const int GPS_READWRITE = 2;

    static PropertyKey AppUserModelId()
    {
        PropertyKey k = new PropertyKey();
        k.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        k.pid = 5;
        return k;
    }

    static IPropertyStore Open(string path)
    {
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, GPS_READWRITE, ref iid, out store);
        return store;
    }

    public static void Set(string path, string id)
    {
        IPropertyStore store = Open(path);
        try
        {
            PropVariant pv = new PropVariant();
            pv.vt = 31;   // VT_LPWSTR; PropVariantClear frees the string with CoTaskMemFree
            pv.ptr = Marshal.StringToCoTaskMemUni(id);
            try
            {
                PropertyKey key = AppUserModelId();
                int hr = store.SetValue(ref key, ref pv);
                if (hr != 0) { Marshal.ThrowExceptionForHR(hr); }
                hr = store.Commit();
                if (hr != 0) { Marshal.ThrowExceptionForHR(hr); }
            }
            finally { PropVariantClear(ref pv); }
        }
        finally { Marshal.ReleaseComObject(store); }
    }

    public static string Get(string path)
    {
        IPropertyStore store = Open(path);
        try
        {
            PropertyKey key = AppUserModelId();
            PropVariant pv;
            int hr = store.GetValue(ref key, out pv);
            if (hr != 0) { Marshal.ThrowExceptionForHR(hr); }
            try { return pv.vt == 31 ? Marshal.PtrToStringUni(pv.ptr) : ""; }   // VT_LPWSTR
            finally { PropVariantClear(ref pv); }
        }
        finally { Marshal.ReleaseComObject(store); }
    }
}
"""


def register_aumid_script(aumid: str, display_name: str, icon: Path | None) -> str:
    """PowerShell that writes the AppUserModelId key: DisplayName, plus IconUri when there is an icon."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$k = '{ps_quote(AUMID_KEY_PARENT)}' + [char]92 + '{ps_quote(aumid)}'",
        "New-Item -Path $k -Force | Out-Null",
        f"New-ItemProperty -Path $k -Name 'DisplayName' -Value '{ps_quote(display_name)}' "
        "-PropertyType String -Force | Out-Null",
    ]
    if icon is not None:
        lines.append(
            f"New-ItemProperty -Path $k -Name 'IconUri' -Value '{ps_quote(str(icon))}' "
            "-PropertyType String -Force | Out-Null"
        )
    return "\n".join(lines)


def register_aumid(runner: Runner, aumid: str, display_name: str, icon: Path | None = None) -> bool:
    """Register ``aumid`` for toasts under HKCU (no admin needed). True on success."""
    meta = {"aumid": aumid, "display_name": display_name}
    if icon is not None:
        meta["icon"] = str(icon)
    script = register_aumid_script(aumid, display_name, icon)
    return run_ps(runner, script, tag="ps:aumid-register", meta=meta).ok


def unregister_aumid(runner: Runner, aumid: str) -> bool:
    """Remove the AppUserModelId registry key. True on success (also when it was not there)."""
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$k = '{ps_quote(AUMID_KEY_PARENT)}' + [char]92 + '{ps_quote(aumid)}'\n"
        "if (Test-Path -LiteralPath $k) { Remove-Item -LiteralPath $k -Recurse -Force }"
    )
    return run_ps(runner, script, tag="ps:aumid-unregister", meta={"aumid": aumid}).ok


def set_shortcut_aumid_script(lnk: Path, aumid: str) -> str:
    """PowerShell that sets System.AppUserModel.ID on ``lnk`` through IPropertyStore."""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -TypeDefinition @'\n"
        f"{_PROPERTY_STORE_CSHARP}\n"
        "'@\n"
        f"[NsAumid]::Set('{ps_quote(str(lnk))}', '{ps_quote(aumid)}')"
    )


def get_shortcut_aumid_script(lnk: Path) -> str:
    """PowerShell that prints System.AppUserModel.ID of ``lnk`` (the read-back used by the tests)."""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -TypeDefinition @'\n"
        f"{_PROPERTY_STORE_CSHARP}\n"
        "'@\n"
        f"[NsAumid]::Get('{ps_quote(str(lnk))}')"
    )


def set_shortcut_aumid(runner: Runner, lnk: Path, aumid: str) -> bool:
    """Stamp ``aumid`` on a shortcut. True on success; False leaves the shortcut as it was."""
    res = run_ps(
        runner, set_shortcut_aumid_script(lnk, aumid), tag="ps:aumid-shortcut",
        meta={"lnk": str(lnk), "aumid": aumid}, timeout=120,
    )
    return res.ok
