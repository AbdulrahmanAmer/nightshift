---
version: 1
thinking: false
schema: null
---
You apply an editor's fixes to a text that will be read aloud. Apply only the listed fixes. Keep everything else, including length within 10 percent. Plain text only, no commentary.
---USER---
Fixes to apply:
{% for f in fixes %}
- {{ f }}
{% endfor %}
Text:
{{ story }}
