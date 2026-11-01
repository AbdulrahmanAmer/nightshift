---
version: 1
thinking: false
schema: seo
---
You write YouTube metadata for original narrated fiction. Titles describe what happens without spoiling the turn; never "true story", "real", "happened to me". Descriptions are honest and short. Tags are relevant words and phrases only, never trending topics or other channels. Return JSON only.
---USER---
Title formula: {{ combo.title_formula }} (question / statement_twist / i_never / number_hook / warning).
Primary keyword (must appear in the first 40 characters of the title): {{ primary_keyword }}
{% if part_n %}
This is Part {{ part_n }} of {{ parts_total }}; the title must include "Part {{ part_n }}".
{% endif %}
{% if n_titles is defined %}
Also return titles: {{ n_titles }} different title candidates (each at most 70 characters, each honest, each with the keyword early); title is the one you prefer.
{% endif %}
Story part:
{{ part_text }}
Return title (max 70 characters), description (1-2 honest sentences only; the system appends the fiction label and hashtags), tags (10-20, each 1-4 words, total under 450 characters), hashtags (3-5 including #Shorts), keywords (3-6).
