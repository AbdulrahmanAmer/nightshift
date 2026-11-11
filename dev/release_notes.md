## Which file do I download?

| File | What it is |
|---|---|
| **`Nightshift-Setup-<ver>.zip`** | **Download this.** The installer bundle: extract it and follow `OPERATOR.md`. |
| `Nightshift-Setup-<ver>.zip.sha256` | Optional checksum to verify the Setup zip. |
| `Nightshift-Setup-<ver>.zip.sig` | The release key's signature of the Setup zip (when this release is signed). Setup does not read it. |
| `Nightshift-Update-<ver>.zip` | Only for **Settings > Update from a file** (or the Update shortcut) on an installed copy, for a PC without internet. |
| `Nightshift-Update-<ver>.zip.sha256` | **Required** with the Update zip: choose the files together in **Settings > Update from a file**; the update is refused without it. |
| `Nightshift-Update-<ver>.zip.sig` | The release key's signature of the Update zip (when this release is signed). **Required** by a Nightshift that knows the release key: keep the three Update files in one folder, do not rename them, and choose all three; an update without it is refused (`NS-E707`) and nothing is changed. |
| `Nightshift-VoiceEngine-win-cuda-x64.zip` | The voice engine; the installer downloads it by itself. Ignore it. |
| Source code (zip, tar.gz) | GitHub adds these automatically. Ignore them. |

Requirements: Windows 10 or 11, an NVIDIA RTX graphics card (16 GB recommended), 32 GB of memory and about 75 GB of free disk space.

Every change in this release is listed in [`CHANGELOG.md`](https://github.com/AbdulrahmanAmer/nightshift/blob/main/CHANGELOG.md); this version's entries follow.
