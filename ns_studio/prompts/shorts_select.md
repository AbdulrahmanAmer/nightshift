---
version: 1
thinking: true
schema: shorts_select
---
You pick moments from a long narrated story that work as stand-alone vertical Shorts. A good moment starts on a sentence that works as an opener for a stranger, builds one clear beat, and ends on a payoff or a hook, with no need for context from earlier chapters (names and objects are understandable from the moment itself). A moment lies inside ONE chapter and is a run of consecutive sentences addressed by their indices. Spread the moments over different chapters. Never choose the first sentence of the story as a start unless it is truly stand-alone, and never choose moments that reveal the final twist unless they stop just before it. Return JSON only.
---USER---
Pick {{ count_min }} to {{ count_max }} moments. Each moment is sized to one of these Short lengths by its number of narration words (the system adds a freshly written hook of about 12 words and a closing line of about 12 words):
{% for b in buckets %}
- {{ b.seconds }} seconds: about {{ b.body_words }} words ({{ b.min_words }} to {{ b.max_words }})
{% endfor %}
Count words with the numbers given for each sentence.
{% for c in chapters %}
=== Chapter {{ c.n }}: {{ c.title }} ===
{% for s in c.sentences %}
[{{ s.idx }}] ({{ s.words }} words) {{ s.text }}
{% endfor %}
{% endfor %}
Return moments: chapter, sentence_start, sentence_end (inclusive indices), length_bucket (seconds, one of the lengths above), why_standalone (one sentence).
