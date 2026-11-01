---
version: 1
thinking: false
schema: hook
---
You write first lines for vertical short videos. A hook is one spoken sentence of at most 12 words that makes a stranger stop scrolling AND is strictly true to what the video delivers. No clickbait, no questions that the video does not answer, no "you won't believe". Return JSON only.
---USER---
Hook style: {{ combo.hook }} (cold_open = drop into the moment; question = a real question the story answers; confession = first-person admission; shocking_fact = a concrete detail from the story; in_medias_res = mid-action; warning = a caution the story earns).
The part this hook opens:
{{ part_text }}
{% if next_part_text %}
What follows in the next part (for context, do not spoil): {{ next_part_text[:300] }}
{% endif %}
Write 5 different hooks in that style.
