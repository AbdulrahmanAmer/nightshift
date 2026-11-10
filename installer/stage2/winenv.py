"""Windows integration through PowerShell (component C1): user environment variable, desktop, shortcuts.

This is the only module that names the home environment variable literally. Scripts are passed with
``-EncodedCommand`` so no quoting can go wrong.
"""

from __future__ import annotations

import base64
from pathlib import Path

import ns_common.testguard as testguard
from installer.stage2.runner import Runner, RunResult, SubprocessRunner

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
    if isinstance(runner, SubprocessRunner):    # a stand-in runner starts nothing and is let through
        testguard.refuse_real_shortcut(None)    # a test run never asks Windows where the real Desktop is
    text = eval_ps_text(runner, "[Environment]::GetFolderPath('Desktop')", tag="ps:desktop")
    return Path(text) if text else None


# WScript.Shell goes through the ANSI code page: a shortcut path with Arabic letters on an English-locale PC
# (or any character outside the system code page) fails or is saved with '?'. IShellLinkW/IPersistFile are
# the Unicode interfaces behind it; WScript.Shell stays as the fallback.
_SHELL_LINK_CSHARP = """
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class NsLink
{
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch,
            IntPtr pfd, int fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out short pwHotkey);
        void SetHotkey(short wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch,
            out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, int dwReserved);
        void Resolve(IntPtr hwnd, int fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }

    [ComImport, Guid("0000010B-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPersistFile
    {
        void GetClassID(out Guid pClassID);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, int dwMode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName,
            [MarshalAs(UnmanagedType.Bool)] bool fRemember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
    }

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLinkObject { }

    public static void Create(string lnk, string target, string args, string workdir, string desc,
        string icon)
    {
        IShellLinkW link = (IShellLinkW)new ShellLinkObject();
        try
        {
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(workdir);
            link.SetDescription(desc);
            if (!string.IsNullOrEmpty(icon)) { link.SetIconLocation(icon, 0); }
            ((IPersistFile)link).Save(lnk, true);
        }
        finally { Marshal.ReleaseComObject(link); }
    }

    public static void Update(string lnk, string args, string workdir, string icon)
    {
        // The changed link is saved beside the shortcut and then swapped over it (File.Replace): a power
        // cut leaves the old shortcut or the new one, never a torn file. The temp file is removed on failure.
        string tmp = System.IO.Path.Combine(System.IO.Path.GetDirectoryName(lnk),
            "~nslnk-" + System.Diagnostics.Process.GetCurrentProcess().Id + ".tmp");
        IShellLinkW link = (IShellLinkW)new ShellLinkObject();
        try
        {
            ((IPersistFile)link).Load(lnk, 0);
            link.SetArguments(args);
            if (!string.IsNullOrEmpty(workdir)) { link.SetWorkingDirectory(workdir); }
            if (!string.IsNullOrEmpty(icon)) { link.SetIconLocation(icon, 0); }
            ((IPersistFile)link).Save(tmp, false);
            System.IO.File.Replace(tmp, lnk, null);
        }
        finally
        {
            Marshal.ReleaseComObject(link);
            if (System.IO.File.Exists(tmp)) { System.IO.File.Delete(tmp); }
        }
    }
}
"""


#: The temp file `update_shortcut` saves beside a shortcut before the swap (left only by a power cut).
LNK_TEMP_GLOB = "~nslnk-*.tmp"


def update_shortcut(
    runner: Runner, lnk: Path, arguments: str, *, workdir: Path | None = None, icon: Path | None = None
) -> RunResult:
    """Change the arguments of an existing .lnk (and its working folder and icon when given); everything
    else it holds stays: its name, target, description and toast identity. The result says why not. The
    file is swapped in whole (a temp file beside it, then a rename over it); a read-only or held shortcut
    is refused and stays as it was.

    Only through IShellLinkW: WScript.Shell goes through the ANSI code page and would save a path with other
    characters wrong, which is worse than leaving the shortcut as it is.
    """
    testguard.refuse_real_shortcut(lnk)     # the same refusal as create_shortcut: one guard, one list
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -TypeDefinition @'",
        _SHELL_LINK_CSHARP,
        "'@",
        f"[NsLink]::Update('{ps_quote(str(lnk))}', '{ps_quote(arguments)}', "
        f"'{ps_quote(str(workdir)) if workdir is not None else ''}', "
        f"'{ps_quote(str(icon)) if icon is not None else ''}')",
    ])
    meta = {"lnk": str(lnk), "arguments": arguments}
    if workdir is not None:
        meta["workdir"] = str(workdir)
    if icon is not None:
        meta["icon"] = str(icon)
    return run_ps(runner, script, tag="ps:shortcut-update", meta=meta, timeout=120)


def create_shortcut(
    runner: Runner, lnk: Path, target: str, arguments: str, workdir: Path, description: str,
    icon: Path | None = None,
) -> bool:
    """Create a .lnk (`icon`, an .ico, becomes its icon). True on success.

    Unicode-safe through IShellLinkW; falls back to WScript.Shell when the compiled helper is unavailable.
    """
    testguard.refuse_real_shortcut(lnk)     # a test run writes no shortcut into a real Desktop or Start menu
    icon_arg = ps_quote(str(icon)) if icon is not None else ""
    icon_part = f"$s.IconLocation = '{ps_quote(str(icon))},0'; " if icon is not None else ""
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  Add-Type -TypeDefinition @'",
        _SHELL_LINK_CSHARP,
        "'@",
        f"  [NsLink]::Create('{ps_quote(str(lnk))}', '{ps_quote(target)}', '{ps_quote(arguments)}', "
        f"'{ps_quote(str(workdir))}', '{ps_quote(description)}', "
        f"'{icon_arg}')",
        "} catch {",
        "  $s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{ps_quote(str(lnk))}'); "
        f"$s.TargetPath = '{ps_quote(target)}'; "
        f"$s.Arguments = '{ps_quote(arguments)}'; "
        f"$s.WorkingDirectory = '{ps_quote(str(workdir))}'; "
        f"$s.Description = '{ps_quote(description)}'; "
        f"{icon_part}"
        "$s.Save()",
        "}",
    ])
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
    testguard.refuse_real_shortcut(lnk)
    res = run_ps(
        runner, set_shortcut_aumid_script(lnk, aumid), tag="ps:aumid-shortcut",
        meta={"lnk": str(lnk), "aumid": aumid}, timeout=120,
    )
    return res.ok
