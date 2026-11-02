# Nightshift

### A.1 One line
**Nightshift works while you sleep: it writes an original story, turns it into a full long-form YouTube video, then cuts the best moments into Shorts that send viewers back to it. Everything runs on your own PC.**

### A.2 Installer welcome screen (exact text shown by `INSTALL.cmd`)
> **Welcome to Nightshift**
>
> Nightshift sets up a story-to-video factory on this PC. It writes an original story, narrates it, adds captions and matching stock footage, and produces **one long-form video (10+ minutes, with chapters and ad-break points)** plus **several vertical Shorts cut from it**, each with its own hook. Everything lands in your Outbox, ready for you to review and post.
>
> Optionally, it also installs **Nightshift Code**, an AI coding assistant in your terminal, powered by the same model running on your graphics card. No account, no subscription, nothing leaves your PC.
>
> **What setup will do:** check your PC (graphics card, driver, memory, disk space), install the free tools Nightshift needs, download the AI models (~20 GB, or copy them from a folder you already have), tune them for your hardware, and run a quick test of everything. You'll answer a few questions along the way. You can close and re-run setup at any time; it picks up where it left off.
>
> **You'll need:** an NVIDIA RTX GPU with 16 GB, ~40 GB of free disk space, an internet connection during setup (or a folder with the model files), and two free stock-footage API keys (Pexels and Pixabay). Setup shows you where to get them.

### A.3 How it installs itself
1. **Checks the PC**: Windows version, NVIDIA GPU, driver, RAM, free disk, internet, power settings. It tells you exactly what to fix if something is missing.
2. **Installs free tools**: Git, Windows Terminal, FFmpeg, Python (via uv). Everything it already finds is skipped.
3. **Asks you a few questions**: first channel name and niche, language, narrator voice (listen to samples), stock-footage keys, how many videos per week, what time to run, whether to install Nightshift Code.
4. **Downloads the AI engine and models** from their official sources, verifying every file.
5. **Tunes itself** to your exact hardware for maximum speed.
6. **Tests everything** and shows a pass/fail checklist.
7. **Creates desktop shortcuts** and a nightly scheduled task that can wake the PC.

### A.4 What it does after install
- **Nightshift Studio** (runs nightly, or on demand): for each channel it
  1. invents an original story idea in your niche and avoids anything it has made before
  2. plans the story in chapters, then writes, critiques and rewrites it until it passes a quality bar
  3. narrates it in the channel's voice and adds captions
  4. finds matching stock footage and edits a **long-form 16:9 video** (10–14 min), with chapters and suggested mid-roll ad points
  5. makes a thumbnail and the long-form title, description and chapter timestamps
  6. picks the strongest **self-contained moments** and turns each into a **vertical Short** with a freshly written and voiced hook, its own vertical edit, and an ending that points to the full story
  7. writes titles, descriptions, tags and hashtags for every Short
  8. checks quality and YouTube-policy safety on everything
  9. drops the long-form video, its Shorts, and all upload details into the Outbox
- **Nightshift Clipper** (in the Dashboard): drop in any long video you own (a stream VOD, a podcast episode, a talking-head recording). Nightshift transcribes it, finds the moments that stand on their own, and cuts each into a vertical clip with captions, using a layout template that fits the video: facecam on top of gameplay, stacked or single-speaker framing for podcasts, tracked crop for talking heads. Every clip gets a title, description and tags and lands in the Outbox like everything else.
- **Nightshift Dashboard** (shortcut, opens in your browser): channels, the review queue with video preview and approve/reject, tonight's run and its log, the Clipper drop zone and templates, analytics import, pause/resume, and settings.
- **You** review each video (`approve` / `reject`) in the Dashboard and upload approved ones in YouTube Studio.
- Over time, you can feed it your YouTube Analytics export so it learns which hooks, styles and lengths work best for each channel.
- **Nightshift Code** (optional shortcut): opens a terminal coding assistant (OpenCode) on the local model. It reads, writes and edits code in your projects folder, fully offline.

### A.5 What it takes in
| Input | When | Required |
|---|---|---|
| Channel name, niche, language | Setup / `studio channel add` | Yes |
| Narrator voice: preset, described in words, or cloned from a 3–10 s clip **you have rights to** | Setup / anytime | Yes |
| Pexels + Pixabay API keys (free) | Setup | Yes |
| Masters per week, run time, publish window | Setup | Defaults provided |
| Optional: seed keywords, topics to avoid, style notes | Channel profile | No |
| Optional: your own example stories | `studio/exemplars/` | No |
| Optional: YouTube Analytics CSV export | Dashboard → Learn, or `studio learn --import` | No |
| Clipper: a video file you own (MP4/MKV/MOV, up to 4 h), a template choice, a target channel | Dashboard → Clipper (drag and drop), or `studio clip <file>` | For clips |
| Coding: your project files and instructions | Nightshift Code | — |

### A.6 What it puts out
| Output | Where |
|---|---|
| **Long-form master**: 1920×1080 MP4, 10–14 min, narration + captions + stock footage, **no music**, chapters | `Outbox\<channel>\<date>\<job>\long\` |
| **Thumbnails**: 3 variants (1280×720) for the long-form | same folder |
| **Shorts**: 4–6 vertical 1080×1920 MP4s cut from the master, each with its own voiced hook and vertical edit | `Outbox\<channel>\<date>\<job>\shorts\` |
| Upload sheet per video (`.json` + copy-ready `.txt`): title, description, tags, hashtags, chapters (long), suggested publish time, suggested mid-roll points (long), "Related video" instruction (Shorts), AI-disclosure answer, "made for kids" answer | next to each MP4 |
| **Clips** from your own videos: 3–8 vertical 1080×1920 MP4s per source video, captions burned in, template layout, each with an upload sheet | `Outbox\<channel>\<date>\clip-<id>\` |
| Daily run report: what was made, what was rejected and why, timings | Dashboard → Runs, `logs\`, `studio status` |
| Coding: code changes in your projects | your Projects folder |


## Developer quick start

Nothing runs on the authoring laptop; CI is the test runner (see `CLAUDE.md`). On a dedicated dev or target machine:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

Run the pipeline without any model or GPU: `studio run --fake-services --format shorts_only --count 1` (Slice 1+).
