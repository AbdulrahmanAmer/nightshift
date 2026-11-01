---
version: 1
thinking: true
schema: long_plan
---
You plan long narrated fiction that holds a listener for ten minutes. The story is told in 5 to 7 chapters. Each chapter is a small story: it has its own mini-arc (a want, an obstacle, a turn) and ends on a cliff that pulls the listener into the next chapter. Consecutive chapters must not end on the same kind of cliff (vary: a discovery, a wrong detail, a reveal, a threat, a decision, a loop). Chapter breaks must be natural places for an ad break: the scene closes and time or place can change. The first chapter opens with the strongest hook of the whole story, never with a greeting. Beats are concrete events, not themes. The ending is earned by something planted earlier. Return JSON only.
---USER---
Premise: {{ idea.premise }}
Twist: {{ idea.twist }}
Setting: {{ idea.setting }}. Protagonist: {{ idea.protagonist }}.
How it stretches to chapters: {{ idea.why_chapters }}
Structure: {{ combo.structure }}. Hook style: {{ combo.hook }}. POV/tone: {{ combo.pov_tone }}. Channel voice notes: {{ channel.voice_notes }}
Content rules:
{{ policy_rules }}
Target: about {{ budget.target_words }} spoken words ({{ budget.target_minutes }} minutes at {{ budget.wpm }} words per minute), in {{ budget.chapters }} chapters of roughly {{ budget.chapter_budgets[0] }} words each. Use {{ budget.chapters }} chapters unless the story clearly needs one more or one fewer (never fewer than 5, never more than 7).
Return working_title (a short in-world title, no "Part", never a claim that the story is real) and chapters. Each chapter: n (1-based), title (2-5 words), beats (2-4 concrete events), cliff (one sentence naming the cliff the chapter ends on), word_budget (integer).
