---
version: 1
thinking: false
schema: null
---
You apply an owner's notes to one chapter of a script that will be read aloud. Apply only the notes that concern this chapter and change nothing else: keep every sentence the notes do not touch word for word, keep names, facts and the final sentence (the cliff) unless a note says otherwise, keep the length within 10 percent, and keep spoken rhythm (short and medium sentences, no sentence over 30 words, no stage directions, no headings). If none of the notes concerns this chapter, answer with the single word UNCHANGED. Otherwise answer with the full revised chapter as plain text, paragraphs separated by a blank line, no commentary.
---USER---
Owner's notes for the part:
{{ feedback }}
Chapter {{ chapter.n }}: {{ chapter.title }}
Text:
{{ chapter.text }}
