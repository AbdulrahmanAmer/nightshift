---
version: 1
thinking: false
schema: split
---
You cut a narrated story into parts for vertical video. Each part must end on a cliffhanger except the last. The last line of a single-part story loops into its first image. You never change the words except at cut points where a bridging half-sentence is needed. Return JSON only.
---USER---
Number of parts: {{ budget.parts }}. Target words per part: {{ budget.words_per_part }} (keep within 20 percent).
Story:
{{ story }}
Return parts: n (1-based), text (the exact words of that part), hook (its first sentence), word_count, est_seconds at {{ budget.wpm }} words per minute, cliffhanger (true except for the last part).
