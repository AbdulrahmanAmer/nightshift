---
version: 1
thinking: false
schema: null
---
You lengthen one chapter of a narrated story by adding sentences. Keep every existing sentence exactly as it is, word for word and in the same order; only insert new sentences between them. The new sentences add sensory detail, the narrator's thoughts, small actions and quiet beats that fit the scene. Do not add plot points, new characters, new facts that later chapters would contradict, or anything that changes the ending line. Keep the voice, tense and reading level. Plain text only, paragraphs separated by a blank line, no commentary.
---USER---
Chapter {{ chapter.n }}: "{{ chapter.title }}".
Add about {{ add_words }} words in total (never fewer than {{ add_words * 3 // 4 }}).
Plot beats that must stay unchanged:
{% for b in beats %}
- {{ b }}
{% endfor %}
Chapter text:
{{ chapter_text }}
