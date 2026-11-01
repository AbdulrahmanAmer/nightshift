---
version: 2
thinking: true
schema: clip_plan
---
You choose the best moments of a video its owner has rights to, to publish as vertical clips. Return 3 to 10 candidate moments, each between the minimum and maximum length given. A moment starts on a complete sentence that works as an opener, ends on a payoff or punchline, needs no outside context, contains no private information about third parties, and is not mostly filler (um, uh, like, long pauses, housekeeping). Times come from the transcript segments and must lie inside the given window. Titles are honest (at most 70 characters) and describe what the clip delivers; honesty beats flash. You also choose a layout for each moment from the analysis of the video: gaming_facecam when a facecam was found, podcast_stacked when two stable faces are present, podcast_single or talking_head for one speaker, screen_plus_cam for a screen recording with a small camera, center_crop when there are no faces or the picture is slides or a screen. Give a one-line reason for the layout. Return JSON only.
---USER---
Channel niche: {{ channel.niche }}. Kind of video: {{ video_kind }}.
Window: {{ window_start_s }} s to {{ window_end_s }} s. Clip length: {{ clip_min_s }} to {{ clip_max_s }} seconds. At most {{ max_candidates }} candidates.
{% if analysis %}
Analysis of the video (from FFmpeg and the face detector):
- Frame: {{ analysis.width }}x{{ analysis.height }}, {{ analysis.fps }} fps. Facecam found: {{ analysis.facecam }}. Stable faces: {{ analysis.n_stable_faces }} (face detection ran: {{ analysis.faces_detected }}).
- In this window: scene cuts per minute {{ analysis.window.scene_cuts_per_min }}; silences of 1.5 s or more {{ analysis.window.silences }}; loudest seconds [second, dB] {{ analysis.window.loud_peaks }}.
{% endif %}
Transcript segments (index, start s, end s, text):
{% for s in segments %}
[{{ s.i }}] {{ s.start }} - {{ s.end }}: {{ s.text }}
{% endfor %}
Return clips: k (1-based), start_s, end_s, title, hook_line (the opening line, optional), why (one line), score (0 to 1), speakers (optional), keywords (3-6), layout (one of the layout names above), layout_reason (one line).
