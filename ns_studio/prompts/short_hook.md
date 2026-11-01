---
version: 1
thinking: false
schema: short_hook
---
You write the first and last lines of a vertical Short cut from a longer story. The hook is one spoken sentence of at most 12 words that makes a stranger stop scrolling AND is strictly true to what the moment delivers; it must make sense without any earlier context. No clickbait, no questions the moment does not answer, no "you won't believe". The closing line is one spoken sentence of at most 15 words that points to the full story on the channel; vary its wording (for example "The full story is on the channel", "Chapter by chapter, the whole story is on the channel"), never a threat, never a claim that the story is real. Return JSON only.
---USER---
Hook style: {{ combo.hook }} (cold_open = drop into the moment; question = a real question the moment answers; confession = first-person admission; shocking_fact = a concrete detail from the moment; in_medias_res = mid-action; warning = a caution the moment earns).
The moment (from chapter "{{ chapter_title }}"):
{{ span_text }}
Why it stands alone: {{ why_standalone }}
Write 5 different hooks in that style, and one closing line.
