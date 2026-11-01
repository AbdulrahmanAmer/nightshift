# Caption and thumbnail fonts

Nightshift ships no font binaries. The installer downloads **Anton** (SIL Open Font License 1.1) from
Google Fonts into this folder (`Anton-Regular.ttf` plus its `OFL.txt`), with the URL and SHA256 pinned in
`manifest.json`.

Until the font is present, the presets fall back to Windows system fonts through `font_fallback` in
`caption_styles.yaml` (`Impact`, `Arial`). The caption builder (`ns_studio/media/captions.py`) checks this
folder for a file whose name starts with the preset's `font` name, so dropping `Anton-Regular.ttf` here is
all that is needed.
