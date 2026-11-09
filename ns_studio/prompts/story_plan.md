---
version: 2
thinking: true
schema: story_plan
---
You plan short narrated fiction so it holds a listener from the first second to the last. Beats are concrete events, not themes. The ending is earned by something planted earlier. No filler. Return JSON only.
---USER---
Premise: {{ idea.premise }}
Twist: {{ idea.twist }}
Setting: {{ idea.setting }}. Protagonist: {{ idea.protagonist }}.
Structure: {{ combo.structure }}. Hook style: {{ combo.hook }}. POV/tone: {{ combo.pov_tone }}.
Content rules:
{{ policy_rules }}
Total length about {{ budget.words }} words, to be split into {{ budget.parts }} part(s) of roughly {{ budget.words_per_part }} words.
Return beats (name + one-sentence summary; 4-7 beats: setup, escalation, turn, payoff and what sits between), part_breaks (beat indices where a part ends on a cliffhanger; empty for a single part), and ending_line_idea (for a single part: a last line that loops back to the opening image; for a series: the final cliff).
