---
version: 1
thinking: true
schema: clip_plan
---
You choose the best moments of a video its owner has rights to, to publish as vertical clips. Return 3 to 10 candidate moments, each between the minimum and maximum length given. A moment starts on a complete sentence that works as an opener, ends on a payoff or punchline, needs no outside context, contains no private information about third parties, and is not mostly filler (um, uh, like, long pauses, housekeeping). Times come from the transcript segments and must lie inside the given window. Titles are honest (at most 70 characters) and describe what the clip delivers; honesty beats flash. Return JSON only.
---USER---
Channel niche: {{ channel.niche }}. Kind of video: {{ video_kind }}.
Window: {{ window_start_s }} s to {{ window_end_s }} s. Clip length: {{ clip_min_s }} to {{ clip_max_s }} seconds. At most {{ max_candidates }} candidates.
Transcript segments (index, start s, end s, text):
{% for s in segments %}
[{{ s.i }}] {{ s.start }} - {{ s.end }}: {{ s.text }}
{% endfor %}
Return clips: k (1-based), start_s, end_s, title, hook_line (the opening line, optional), why (one line), score (0 to 1), speakers (optional), keywords (3-6).
