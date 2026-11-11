# K8: one real paste into YouTube Studio (box F-F17, card posting-sheet-true)

Nightshift never uploads. This step is the only proof that YouTube Studio accepts an upload sheet exactly as
Nightshift writes it. It takes about 10 minutes, and nothing is ever made public.

You need: the kit's video in the Outbox (`outbox/gpu-kit/...`), its `.txt` sheet next to it, the Queue page
open in Nightshift, and YouTube Studio (https://studio.youtube.com/) signed in to the channel you test with.

## Steps

1. In YouTube Studio press Create, then Upload videos, and drag in the kit's `.mp4`.
2. Visibility: choose **Private**. Never Public or Unlisted. Do not schedule it.
3. Title: press Copy next to "Title" in the Queue and paste it into Studio's Title box.
4. Description: press Copy next to "Description" in the Queue and paste it into Studio's Description box.
   This copy is the description, then a blank line, then the hashtags, exactly as the `.txt` sheet has them.
5. Tags: press "Show more" in Studio, then press Copy next to "Tags" in the Queue and paste into the Tags box.
6. Audience: answer "made for kids" as the sheet's `Made for kids:` line says.
7. Altered content: answer as the sheet's `Altered or synthetic content:` line says. Write down what the field is
   called in Studio (the help pages name it "Altered content" on one page and "AI use" on another).
8. Save as Private. Do not publish. You may delete the private video afterwards.

## What YouTube must accept (answer each: y, n with what Studio said, or later)

1. The title was accepted as pasted: not shortened, no "Invalid characters" message.
2. The description was accepted as pasted: Studio's counter did not pass its limit, nothing was cut, and the
   chapter list (a long video) shows as chapters after saving.
3. The tags were accepted as pasted: every tag is there and Studio's counter is at or under 500.
4. The altered-content answer could be set as the sheet says.
5. The made-for-kids answer could be set as the sheet says.

## Extra checks (5 minutes, on the same private video; write down what Studio shows)

These settle what the help pages do not say. Undo each one before saving, or delete the video.

- E1 title counting: replace the title with one emoji followed by 99 letters `a` (100 characters, or 101 if
  an emoji counts as two). Write down Studio's counter (100/100 or 101/100) and whether Save is allowed.
  Nightshift counts an emoji as two until this answer says otherwise (R2 in `youtube_rules.py`).
- E2 tag counting: replace the tags with this line (500 characters as YouTube's own rule counts them: a tag with
  a space counts its two quote marks, and every comma counts). Write down Studio's counter and whether all ten
  tags stay:
  `0 tttttttttttttttttttttttttttttttttttttttttttttttt, 1 tttttttttttttttttttttttttttttttttttttttttttttttt, 2 tttttttttttttttttttttttttttttttttttttttttttttttt, 3 tttttttttttttttttttttttttttttttttttttttttttttttt, 4 tttttttttttttttttttttttttttttttttttttttttttttttt, 5 tttttttttttttttttttttttttttttttttttttttttttttttt, 6 tttttttttttttttttttttttttttttttttttttttttttttttt, 7 tttttttttttttttttttttttttttttttttttttttttttttttt, 8 tttttttttttttttttttttttttttttttttttttttttttttttt, uuuuuuuuuuuuuuuuuuuuuuu`
- E3 line ends: copy the description from the `.txt` sheet in Notepad instead of the Queue and paste it over the
  first one. Write down Studio's description counter for both copies (Nightshift counts each line break as two
  bytes, the Windows form, so either copy stays within 5,000).

- E4 hashtag with a hyphen: add `#true-crime` at the end of the description, save, and write down which part
  of it YouTube shows as a link after saving (all of it, or only `#true`). Nightshift writes it `#truecrime`.

## What to record (the kit writes it to `paste.json`)

- the five answers above (y, n with Studio's words, or later), the field name from step 7, and E1 to E4;
- the Nightshift version and the output id of the video (the Queue shows both);
- if anything was refused: the exact text Studio showed. The builder of the next release changes the rule in
  `ns_studio/media/youtube_rules.py` (one constant, with the page and date it came from).
