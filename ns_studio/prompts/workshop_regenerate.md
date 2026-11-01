---
version: 1
thinking: true
schema: workshop_concepts
---
You rewrite one concept of a narrated fiction series with the owner's notes. Apply every note; change nothing the notes do not touch unless it is needed to keep the concept consistent. The result stays original fiction: no real people, brands, crimes or tragedies, nothing presented as true, no graphic violence. Keep the same letter. The arc stays exactly five beats and the concept must still carry a multi-part series. Return JSON only with one concept in the list.
---USER---
Channel: {{ channel.display_name }}. Niche: {{ channel.niche }}. Style notes: {{ channel.voice_notes }}
Content rules:
{{ policy_rules }}
Current concept {{ letter }}: "{{ concept.title }}"
Premise: {{ concept.premise }}
Cast: {{ concept.cast | join("; ") }}
Setting: {{ concept.setting }}. Tone: {{ concept.tone }}
Beats:
{% for b in concept.beats %}
{{ loop.index }}. {{ b }}
{% endfor %}
Why it works: {{ concept.why_it_works }}
Series potential: {{ concept.series_potential }}
{% if concept.feedback %}
Earlier notes already applied:
{% for f in concept.feedback %}
- {{ f }}
{% endfor %}
{% endif %}
Owner's notes to apply now:
{{ feedback }}
Return concepts: a list with exactly one item in the same shape as before (letter "{{ letter }}", title, premise, cast, setting, tone, beats with exactly 5 sentences, why_it_works, audience, series_potential, risks).
