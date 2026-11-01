---
version: 1
thinking: false
schema: chapter_draft
---
You write one chapter of a long story to be read aloud. Spoken rhythm: short and medium sentences, concrete nouns, one tense kept consistent, no stage directions, no headings, no chapter numbers in the text. Never claim the story is real. Avoid stock phrases and cliches. Keep names, places, objects and the timeline exactly consistent with the plan and with the summaries of earlier chapters; do not re-introduce the premise. Reading level grade 5-8, no sentence longer than 30 words. Return JSON only: text (the chapter, paragraphs separated by a blank line) and summary (two sentences of plain facts a later chapter must remember: names, objects, injuries, times, what the narrator now knows).
---USER---
Channel voice notes: {{ channel.voice_notes }}
POV/tone: {{ combo.pov_tone }}. Pacing: {{ combo.pacing }}. Structure: {{ combo.structure }}.
Story: {{ idea.premise }} Twist: {{ idea.twist }}
{% if summaries %}
What has happened so far (summaries of earlier chapters):
{% for s in summaries %}
Chapter {{ s.n }}, {{ s.title }}: {{ s.summary }}
{% endfor %}
{% endif %}
{% if last_sentences %}
The previous chapter ended with these sentences, verbatim (continue naturally from them, do not repeat them):
{{ last_sentences | join(" ") }}
{% endif %}
Now write chapter {{ chapter.n }} of {{ plan.chapters | length }}: "{{ chapter.title }}".
Beats to cover:
{% for b in chapter.beats %}
- {{ b }}
{% endfor %}
The chapter must end on this cliff, as its last sentence: {{ chapter.cliff }}
{% if chapter.n == 1 %}
This is the opening chapter: its first sentence is the story's hook and must be true to the story.
{% endif %}
{% if chapter.n == plan.chapters | length %}
This is the final chapter: pay off the twist and end on a line that echoes the story's first line.
{% endif %}
Length: about {{ budget.words }} words (between {{ budget.min_words }} and {{ budget.max_words }}).
