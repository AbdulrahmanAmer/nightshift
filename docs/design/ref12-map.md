# ref12 design map

Reference: `D:\NightShift-refs\ref-12-dashboard.png`, a 1024×768 render of a Dribbble dashboard. Everything below was measured on that frame with 2× nearest-neighbour crops and a 50 px guide grid; colours were sampled from pixels. Each measurement is given twice:

- **ref**: pixels in the 1024×768 render.
- **1440**: the original 1440×1080 design frame (ref × 1.406), snapped to the 4 px grid. These are the values the code uses.

Code: tokens in `ns_dashboard/ui/src/styles.css` (the `--r12-*` block), components in `ns_dashboard/ui/src/components/ref12/`, review pages `#/design` (component sheet) and `#/design/ref12` (the whole screen rebuilt with Nightshift content).

The left side tab is out of scope here. It is built on `feat/ui-sidetab` (`shell/SideTab.tsx`). The recreation reserves its 140 px column as a dashed slot.

---

## 1. Frame and layers (depth map)

| z | Layer | Box ref (x, y, w, h) | Box 1440 | Background | Edge | Shadow (x y blur colour) |
|---|---|---|---|---|---|---|
| 0 | **PageBackdrop** | 0, 0, 1024, 768 | 0, 0, 1440, 1080 | `#ced0e2` lavender grey | none | none |
| 0.5 | Backdrop decoration | hairline curves at top right (900→1024, y 0–25), left (0→130, y 170–215), left loop (0–110, y 500–620), bottom (240–600, y 735–768), right loop (990–1024, y 400–500); dots: teal 7 px at (22, 586), teal 4 px at (935, 22), purple 9 px at (990, 511); outline ring 25 px at (55–80, 720–745), filled slate disc 20 px at (918–938, 725–745) | ×1.406 | white curves at ~55% opacity, 1–1.5 px | none | none |
| 1 | **AppWindow** | 44, 34, 936, 699 | 62, 48, 1316, 983 | `#f7f8fb` | 1 px inner white highlight, radius ref 24 / **1440 32** | `0 24 64 rgba(62,64,120,.14)` (soft, wide, cool) |
| 2 | **SideTab layer** (not built here) | 44, 34 → ~112 wide, purple wave from y≈160 to y≈555 | 62, 48, 140 col | `#716eee` → `#5650d4` (top → bottom) | wave curves | none |
| 3 | **Content layer**: body cards | see grid | see grid | `#ffffff` | none | `0 2 6 rgba(30,32,80,.03)` (barely there) |
| 3.5 | Inset tiles inside cards (tint wells, swipe tray, dashed adders) | — | — | `#f1f1fd` / `#f3f1fb` / `#e4e3f4` | dashed `#c5c6ee` 1.5 px on adders | none |
| 4 | **Raised rows**: Message row, Assignment item, current Today-task row | — | — | `#ffffff` | none | `0 8 24 rgba(30,32,80,.08)` |
| 5 | **Raised tiles**: three FeatureCards (the last crosses the window's right edge by 12 ref / 18 px) | — | — | `#fdfcff` | none | `0 12 32 rgba(30,32,80,.10)`, `0 2 6 rgba(30,32,80,.04)` |
| 6 | **FloatingEventCard** (overlaps the Notifications card's left edge by 14 ref / 20 px) | 131, 326, 218, 65 | 184, 458, 306, 92 | `#ffffff` | none, radius 13 ref / 20 | `0 16 40 rgba(30,32,80,.14)`, `0 4 10 rgba(30,32,80,.06)` |
| 7 | Badges and dots on top (status dot, active calendar day, avatar plus, kbd chip) | — | — | accent / green | 2 px ring in card colour on avatars | active day: `0 6 14 -4 accent` |

The PremiumCard is on layer 3 but is the only saturated surface (`#6f6bf0`); it is taller than its row neighbours by 4 px at the top.

## 2. Grid

**Window insets** (from the window's own edge): side tab column 101 ref / **140**, top 28 ref / **40**, right 35 ref / **48**, bottom 30 ref / **42**. Content width 800 ref / **1128**.

**Top bar**: y 62–86 ref (24 tall) / **36 tall** at window y 40.

**Row A, hero** (top 114 ref / **160 abs**, height 132 ref / **184**):

| Cell | ref x–x (w) | 1440 w | Gap after |
|---|---|---|---|
| HeroGreeting | 144–400 (256) | 360 (404 incl. badges) | 31 ref / 44 |
| DashedAddCard | 431–561 (130) | 184 | 14 ref / 18 |
| FeatureCard 1 | 575–705 (130) | 184 | 13 / 18 |
| FeatureCard 2 | 718–848 (130) | 184 | 14 / 18 |
| FeatureCard 3 | 862–992 (130) | 184 | crosses the window edge |

Hero → body gap: 32 ref / **48**.

**Body: three unequal columns** (top 278 ref / **391 abs**):

| Column | ref x–x (w) | 1440 w |
|---|---|---|
| col 1 | 145–400 (255) | **358** |
| col 2 | 415–669 (254) | **358** |
| col 3 | 685–945 (260) | **366** (1fr) |
| gutters | 15–16 | **22** |

Rows are masonry, not a strict table:

| Stack | Items, top→bottom (ref height → 1440) | Vertical gaps |
|---|---|---|
| col 1–2, row B | Notifications 204 → **287**, Assignments 204 → **287** | — |
| col 1–2, row C | TodayTasks (w 380 → **534**) 203 → **286**, PremiumCard (w 129 → **181**) 206 → **290**, 16 ref / **23** apart | row B → C: 17 ref / **20** |
| col 3 | Calendar 215 → **302**; StatRing ×2 (w 125 → **176**, 12 ref / **17** apart) 82 → **120**; BoardMeeting 94 → **128** | 17 / **24** each |

**Card padding**: 18–20 ref → **24** (20 on compact stat cards and under 640 px). Inner rows **16**. Title row is **36** tall (28 line + action).

**Rhythm**: every 1440 value is on a 4 px grid; 2 px only for hairlines, underline and small nudges inside controls.

## 3. Component inventory

Boxes are ref (x, y, w, h). Type is given in 1440 px. Reference type below 11 px in the 1440 frame was raised to 11–12 px for legibility (the reference renders meta at 6–7 ref px, about 9–10 px at 1440).

### TopNav (`TopNav.tsx`)
- Box 144, 64, 230, 22. Tabs `Dashboard`, `Workflows`, `Integrations`, each **16 px icon + 14/20 medium**, 6 px icon gap, **24** between tabs.
- Active: ink `#101339` text, **2 px ink underline** at the bottom of a 36 px hit box (ref y 82). Inactive: text-2 `#3f4053`.
- `TopNav.tab`, `TopNav.tab.icon`, `TopNav.tab.underline`.

### CommandSearch (`CommandSearch.tsx`)
- Box 387, 62, 150, 24 → **212×36**, radius 8 ref / **12**, fill white, border `rgba(16,19,57,.14)` 1 px, 16 px search icon at 12 px, placeholder 13 px muted.
- States: focus = accent border + 3 px accent-soft ring.

### ThemeSegmentedToggle (`SegmentedToggle.tsx`, variant `tray`)
- Box 631, 62, 84, 24 → **118×36**. Tray `#e4e3f4`, radius **10**, 3 px inset. Items 30 px tall, radius 8, 10 px side padding, 14 px icon (sun / moon) + 12 medium.
- Active item: accent fill `#6f6bf0`, white text (ref 40 wide → 56). Inactive: text-2. Motion: colour fade 200 ms; a sliding thumb is optional.

### IconButtons (`IconButton.tsx`)
- Bell at x 733, gear at x 759, 16 ref icons → **20 px icons in 36 px hit areas**, no fill, text-2; hover tint-2.
- `Kebab`: 28 px round, horizontal dots in lists, vertical dots on agenda rows.

### ExportButton with KbdChip (`ExportButton.tsx`, `KbdChip.tsx`)
- Box 779, 62, 81, 24 → **114×36**, tint-2 tray, radius 10, 14 px down arrow, 13 px text-2 label.
- `ExportButton.chip`: 842–858 ref → **22×22**, radius 6, fill `#9ea0ff`, 11 px white mono.

### PrimaryButton "Add new board" (`ActionButton.tsx`, variant `ink`)
- Box 870, 62, 80, 24 → **112×36**, radius 7 ref / **10**, fill `#101339`, white 13 medium.
- Variants share the same metrics: `accent` (Accept invite, Check), `soft` (Reschedule: `#e3e3ef` fill, ink text), `ghost` (Edit, Share, Clear). Size `sm` = 28 px, radius 8, 12 px.

### HeroGreeting (`HeroGreeting.tsx`)
- Heading box 144, 115, 256, 90. Three lines at 30 ref pitch → **36/44 semibold**, ink, tracking −0.01 em (display size only). Line 1 carries the badge cluster 12 px after the text.
- `HeroGreeting.badges`: two 17 ref → **28 px** round chips, white with hairline, accent glyph, overlapping 6 px.
- `HeroGreeting.body`: two lines at 14.5 ref pitch → **16/22** text-2, 16 px below the heading, max 400 px.

### AvatarStack (`AvatarStack.tsx`)
- Next to "Today tasks": three **24 px** faces overlapping 8 px, each with a 2 px card-colour ring, then a 24 px accent bubble with a white plus (or `+N`).
- Avatar sizes in the reference: 24 (stack), 20 (assignee), 14 ref glyph chips.

### AddCard (`DashedAddCard.tsx`)
- Box 431, 114, 130, 131 → **184 square**, radius 16 ref / **24**, fill `#f1f1fd`, **1.5 px dashed `#c5c6ee`**, centred plus tile 17 ref → **28 px**, radius 8, accent fill, white 16 px plus.

### FeatureCard ×3 (`FeatureCard.tsx`, `Illustrations.tsx`)
- Boxes 575/718/862, 114, 130, 132 → **184 square**, radius **24**, fill `#fdfcff`, raised shadow, padding 13 ref / **16**.
- `FeatureCard.illustration`: top ~65% of the tile, max **108 px** tall.
- `FeatureCard.title`: 10 ref → **15/20 semibold** ink. `FeatureCard.subtitle`: 7 ref → **12/16** faint `#b8bacb`.
- Hover: lift 2 px, shadow steps up.

### NotificationsCard (`NotificationsCard.tsx`)
- Box 145, 278, 255, 204 → **358×287**, radius 20 ref / **28**, padding 24.
- `NotificationsCard.header`: "Notifications" **20/28 semibold**; `Clear` ghost sm with a 14 px trash icon, muted.
- List gap **16**; overflow visible so the floating card can leave the card.

### FloatingEventCard (`FloatingEventCard.tsx`)
- A list row in its **swiped** state. Card 131, 326, 218, 65 → **306×92**, radius **20**, padding 16/20, float shadow (layer 6).
- Behind it, `FloatingEventCard.tray` fills the row slot (to x 382 ref): `#f3f1fb`, radius 20, with **trash** and **pencil** 16 px ink icons stacked at x 365 ref (32 px buttons). The card sits **44 px** left of the list edge and stops **48 px** short of the right edge.
- `.title` "Upcoming event" **15/20 semibold** + 6 px green dot `#5dac92`.
- `.meta` "Landing design meeting | Time: 120 min": **12/16 muted**, hairline divider.
- `.info` row: 14 px calendar icon + "Sat, 10 May", 14 px clock + "11 AM - 11:45 AM", **12 medium text-2**, 24 px between groups.
- `.kebab`: horizontal, top right.

### MessageRow (`MessageRow.tsx`)
- Box 165, 401, 215, 62 → **302×~88**, white, radius **16**, row shadow, padding 12/16. Indented 20 ref / **20** from the list's left edge.
- `.title` "Message | Product design" **14 semibold**, hairline-colour pipe.
- `.from` 14 px mail icon + "Message from Ken Smith" **12 muted**.
- `.preview` well: 440–453 ref → **24 tall**, radius 6, fill `#e8e8f4` / tint, one truncated **11 px** faint line.

### AssignmentsCard (`AssignmentsCard.tsx`)
- Box 415, 278, 254, 204 → **358×287**, header "Assignments" + `Edit` (14 px pencil).
- `AssignmentItem`: 434, 318, 216, 92 → **304×130**, white, radius **16**, padding 16, row shadow.
  - `.tags` "Motion design" (accent ink `#5651d6`) and "Logo" (ink), **12 medium**, 12 apart; kebab right.
  - `.title` two lines **15/20 semibold**.
  - `.priority` chip "High": 605–641 × 356–370 ref → **24 tall**, radius 8, fill `#fadbe0`, text `#c0263f` **12 medium**.
  - `.tag` chip "Package design": solid `#71baab`/green, white **12 medium**, **28 tall**, radius 8.
  - `.assignee` "Rachel Lee" **12 muted** + 20 px avatar, right.
- `DashedAddButton` "Add new assignment": 434, 424, 216, 39 → **h 56**, radius **16**, `#efeffb` fill, dashed `#c5c6ee`, 24 px plus tile, **13 medium** ink label.

### CalendarCard (`CalendarCard.tsx`, `AgendaTimeline.tsx`)
- Box 685, 278, 260, 215 → **366×302**.
- `.header` "May 2021" **20/28 semibold**; prev/next 14 ref → **28 px** round buttons, tint fill, 14 px chevrons, 4 apart.
- `WeekStrip`: 7 equal columns. Weekday "Mon…Sun" 6 ref → **12/16 muted**; day number **13 semibold** ink; 4 px between them.
  - Active day "18": 17 ref → **28 px** accent circle, white number, soft accent drop shadow. Marked days: 4 px accent dot (Nightshift addition).
- `AgendaTimeline`:
  - `AgendaSlotLabel` "04:30-05:00 PM": **14/20 semibold** ink, then a **dotted rule** to the right edge (2 px dots, 6 px pitch, muted).
  - `AgendaEventRow` (48 tall): icon tile 19 ref → **32 px**, radius 8, tint-2 fill, 16 px ink icon; title **14 semibold**; meta "12:00 - 12:30 • UX/UI design" **12 muted** with a 6 px tone dot (green / red); vertical kebab right.
  - Slot pitch 62 ref; label → event 4, slot → slot **12**.

### TodayTasksCard (`TodayTasksCard.tsx`, `TaskRow.tsx`, `ProgressBar.tsx`)
- Box 145, 499, 380, 203 → **534×286**.
- `.header` "Today tasks" **20 semibold** + AvatarStack. Right: view switch (`SegmentedToggle` variant `icons`: 3 × 28 px, active = accent circle with white icon), `Edit`, `Share` ghost sm with 14 px icons.
- `TaskRow` pitch 45 ref → **64** (12/16 padding, 20 + 16 text lines), radius **16**.
  - `.name` "Conduct research" **14 semibold** ink; `.date` "4 May, 09:20 AM" **12 muted**.
  - `.metric` "Duration" **11 muted** over "02 h 45 m" **14 semibold** tabular.
  - `.progress` "90%" **12 semibold** tabular + **4 px track** 35 ref / **72** wide, track tint-2, accent fill.
  - `.counts` 14 px muted icons (attachment, comments / calendar) + **12 semibold** values separated by a 3 px ink dot.
  - Current row (first) is **raised**: white + row shadow; others are flat and raise on hover.
  - Columns (≥ 448 px container): 1.35fr / 0.9fr / 1.1fr / auto. Narrower: two-line wrap.

### PremiumCard (`PremiumCard.tsx`)
- Box 541, 496, 129, 206 → **181×290**, radius 20 ref / **28**, fill `#6f6bf0`, padding 20, centred column, 12 gaps.
- `.illustration` ~70 ref → **96 px** tall: navy line art with navy fills and white highlights.
- `.title` "Go premium!" **20/28 semibold** white. `.body` three lines **13/18** white 85%.
- `.action` "Find out more": 570–641 × 652–675 ref → **36 tall**, radius 10, navy `#101339` fill (constant in both themes), white 13 medium.

### StatRingCard ×2 (`StatRingCard.tsx`, `RingProgress.tsx`)
- Boxes 685/822, 510, 125, 82 → **176×120**, radius **28**, padding **16**.
- `RingProgress`: 32.5 ref → **48 px**, **4 px** stroke, track tint-2, arc from 12 o'clock with a round cap, green `#5dac92` / crimson `#a3172f`; "90%" **12 semibold** tabular in the centre.
- `.eyebrow` "DATA RESEARCH" / "UX/UI DESIGN": tone-coloured, **12 medium**. The reference uses tracked uppercase; the code uses sentence case (taste rule).
- `.title` "Marketing" **15 semibold**. `.meta` two lines **12/16 muted**.
- `.action` "Check": 904–936 × 566–581 ref → **28 tall** accent sm button, radius 8.

### BoardMeetingCard (`BoardMeetingCard.tsx`)
- Box 685, 608, 262, 94 → **368×128**, padding 20.
- `.title` "Board meeting" **22/28 semibold**; `Edit` right.
- `.date` 10 px light-accent dot `#9ea0ff` + "March 24 at 4:00 PM" **12 muted**.
- `.description` two lines **13/18** text-2.
- `.actions` "Reschedule" (soft: `#e3e3ef`, ink) + "Accept invite" (accent, white): **32 tall**, radius 10, 8 apart.

### Decorative background elements (`AppWindow.tsx` → `PageBackdrop`)
See layer 0.5 in the depth table. All aria-hidden.

## 4. Design tokens

### Palette (sampled)

| Token | Light | Dark (derived) | Where |
|---|---|---|---|
| `--r12-page` | `#ced0e2` | `#0f0d16` | backdrop |
| `--r12-window` | `#f7f8fb` | `#15131d` | window |
| `--r12-card` | `#ffffff` | `#1e1b2a` | cards, rows |
| `--r12-card-2` | `#fdfcff` | `#221f30` | feature tiles |
| `--r12-tint` | `#f1f1fd` | `#24213a` | dashed adders, wells |
| `--r12-tint-2` | `#e4e3f4` | `#2c2943` | trays, soft buttons, tracks |
| `--r12-tint-3` | `#f3f1fb` | `#262335` | swipe tray |
| `--r12-dash` | `#c5c6ee` | `#4a4672` | dashed borders |
| `--r12-ink` | `#101339` | `#eceaf6` | primary text, ink button |
| `--r12-navy` | `#101339` | `#101339` | ink on the accent card |
| `--r12-text-2` | `#3f4053` | `#bebcd0` | body text |
| `--r12-muted` | `#8d8fa5` | `#8b88a3` | meta |
| `--r12-faint` | `#b8bacb` | `#6c6984` | subtitles |
| `--r12-accent` | `#6f6bf0` | `#7c78f5` | the one purple |
| `--r12-accent-ink` | `#5651d6` | `#aeabff` | purple as text |
| `--r12-accent-soft` | `#e6e5fd` | accent 18% | soft fills |
| `--r12-accent-2` | `#9ea0ff` | `#8e8bff` | kbd chip, date dot |
| `--r12-green` / `-ink` / `-soft` | `#5dac92` / `#2f7d64` / `#e3f2ed` | `#5fbf9f` / `#7fd3b6` / 16% | ring, tag chip, status |
| `--r12-red` / `-ink` / `-soft` | `#a3172f` / `#c0263f` / `#fadbe0` | `#e5566d` / `#f08a9a` / 16% | ring, priority chip |
| side tab (not built here) | `#716eee` → `#5650d4` | — | SideTab |

Raw samples: heading ink `#020024`–`#101339`, accent `#706cef`/`#6f6bee`, green chip `#71baab`, green ring `#5dac92`, red ring `#94081c`, high chip `#f9dae0`, tray `#e4e3f4`/`#e2e4f4`, preview well `#e8e8f4`, reschedule `#e3e3ef`.

### Type scale (1440)
The reference face is a geometric grotesk close to Gilroy. The code keeps the project's Geist (no new font dependency); see deltas.

| Role | Size/line | Weight |
|---|---|---|
| Display (hero) | 36/44 (28/36 mobile) | 600 |
| Card title large | 22/28 | 600 |
| Card title | 20/28 | 600 |
| Tile / row title | 15/20 | 600 |
| Nav, row name, agenda | 14/20 | 500–600 |
| Button, body small | 13/18 | 500 |
| Meta, chips, eyebrow | 12/16 | 400–500 |
| Smallest (well text, metric label, kbd) | 11/16 | 400 |

Weights 400, 500, 600 only. Tabular figures on every number.

### Radius scale
window **32** · card **28** · tile **24** · floating card / tray **20** · row **16** · search **12** · control **10** · chip / small button / icon tile **8** · kbd / well **6** · round (avatars, rings, day circle).

### Spacing scale
4-based: **4, 8, 12, 16, 20, 24, 28, 36, 40, 44, 48**. Gutters 18 (tiles), 22 (columns), 24 (stack in col 3). Card padding 24 / 20 / 16.

### Shadow scale
`--r12-shadow-card` `0 2 6 /.03` · `--r12-shadow-row` `0 8 24 /.08` · `--r12-shadow-raised` `0 12 32 /.10 + 0 2 6 /.04` · `--r12-shadow-float` `0 16 40 /.14 + 0 4 10 /.06` · `--r12-shadow-window` `0 24 64 rgba(62,64,120,.14)` + inner white edge. All neutral cool, never coloured (the active calendar day is the one exception). Dark theme: cards drop shadows; depth comes from surface steps.

### Icon sizes
14 (meta, inline actions), 16 (row icons, nav), 20 (top bar). lucide, stroke 1.75.

### Avatar sizes
20 (assignee), 24 (stack), 28 (hero badges), 32 (large).

### Illustration style
Flat line art: **2 px round ink strokes**, one flat **accent** fill per object, a **pale accent tint** for back planes, white paper, small sparks and crosses around the object, slight tilt. On the accent card the strokes and fills turn navy with white highlights. `Illustrations.tsx` holds four matching spot drawings (Write a story, Cut clips, Build a series, Train a voice); a designer can replace them later at the same 120×100 viewBox.

## 5. Interaction and motion

- **Hover lift** on FeatureCards: translateY −2 px + raised shadow, 160 ms ease-out (`.r12-lift`). Rows raise on hover (white + row shadow), not lift.
- **Swipe to reveal** on notification rows: the card translates left 44 px and lifts to the float layer, uncovering delete and edit. Rest state = flat row.
- **Toggle**: Light/Dark and the view switch fade the active fill in 200 ms; a sliding thumb (transform only) is the natural upgrade.
- **Progress fill** animates `transform: scaleX` (500 ms); **rings** animate `stroke-dashoffset` (600 ms). Never width.
- **Press**: buttons scale to 0.98.
- **Calendar**: picking a day moves the accent circle; agenda content should cross-fade.
- Focus: 2 px accent outline, 2 px offset, on every control. `prefers-reduced-motion` turns lifts off.

---

## Applying to Nightshift pages

Not implemented in this PR. Each page keeps its purpose from `BUILD_SPEC.md` §20.2; the ref12 shell (window, TopBar, side tab) wraps all of them, and the body uses the three-column grid (358 / 358 / 1fr at 1440, 22 gutters) collapsing to one column under 1024 px.

| Page | ref12 components | Grid |
|---|---|---|
| **Queue** (home) | HeroGreeting (tonight's count), FeatureCard-sized media tiles for the ready filmstrip, AssignmentItem as the posting checklist steps, DashedAddButton-styled "Paste the YouTube link", ActionButton accent/soft for Approve/Reject, KbdChip for A / R / → | Row A: player spans col 1–2, checklist card col 3 (tall). Below: filmstrip in square tiles |
| **Create** | DashedAddCard + FeatureCards for the two choices (A new story video / Clips from my own video), TodayTasksCard with TaskRows for jobs (progress + stage), NotificationsCard for "Earlier" with plain failure reasons | Row A tiles; body col 1–2 = job list, col 3 = choices form |
| **Library** | TopNav-style tabs (Ideas, Stories, Voices, Formats, Footage), AssignmentItem cards for ideas with Chip statuses, FeatureCard tiles for formats, MessageRow for stories, PremiumCard as the "Design or clone a voice" promo | 3-col card grid; Voices uses col 3 for the PremiumCard |
| **Assistant** | NotificationsCard-style thread, FloatingEventCard as the confirm card (raised, with actions), CommandSearch-style input | Chat col 1–2, context card col 3 |
| **Channels** | Card per channel with AvatarStack, CalendarCard + AgendaTimeline for schedule, SegmentedToggle for Pause/Resume, Chip pools | 3-col cards; editor opens col 1–2 with schedule in col 3 |
| **Results** | StatRingCard pairs (QC pass rate, retention target), TaskRow-style ranked lists with ProgressBar, BoardMeetingCard for the go/no-go milestone, DashedAddCard as the CSV drop zone | Row A stats; body col 1–2 lists, col 3 milestone + changes |
| **Settings** | Card sections with ActionButtons, SegmentedToggle (theme, notifications), CalendarCard WeekStrip for run time, ExportButton for diagnostics | Single column of cards at 1440 max 2 cols (col 1–2 + col 3 for Update/Diagnostics) |

Notes:
- The top bar keeps the engine status pill from today's Strip (it moves into TopBar's action group).
- Keep one PremiumCard-style accent surface per page at most; everything else stays on neutral cards.
- Mobile (390): window radius and backdrop drop away, side tab becomes the bottom bar, every grid stacks in reading order; components are already checked at 390 on `#/design`.
