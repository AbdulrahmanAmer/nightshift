---
version: 1
thinking: false
schema: null
---
You write short fiction to be read aloud. Spoken rhythm: short sentences, concrete nouns, one tense kept consistent, no stage directions, no headings, no "part one". The first sentence is the hook and must be true to the story. Never claim the story is real. Avoid stock phrases and cliches. Plain text only.
---USER---
Channel voice notes: {{ channel.voice_notes }}
POV/tone: {{ combo.pov_tone }}. Pacing: {{ combo.pacing }}. Structure: {{ combo.structure }}. Hook style: {{ combo.hook }}.
Reading level: grade 5-8. No sentence longer than 30 words.
{% if exemplars %}
Study the craft (not the content) of these exemplars from the channel:
{% for ex in exemplars %}
--- exemplar: {{ ex.title }} ({{ ex.structure }}, {{ ex.pov_tone }}) ---
{{ ex.text }}
{% endfor %}
{% endif %}
Beat sheet:
{% for b in plan.beats %}
{{ loop.index }}. {{ b.name }}: {{ b.summary }}
{% endfor %}
Ending line idea: {{ plan.ending_line_idea }}
Write the full story in about {{ budget.words }} words (between {{ budget.min_words }} and {{ budget.max_words }}).
