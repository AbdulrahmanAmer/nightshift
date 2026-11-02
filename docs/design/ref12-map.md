# ref12 design map

Reference: `D:\NightShift-refs\ref-12-dashboard.png`, a 1024×768 render of the Planify dashboard on Dribbble.

Source of truth: the **Planify UI** library at `D:\NightShift-tools\planify-ui`, a component library built from this same reference. Its tokens and components are ported here onto our Tailwind v4 setup:

- Tokens: the `--ds-*` block in `ns_dashboard/ui/src/styles.css`, exposed to Tailwind as `bg-ds-*`, `text-ds-*`, `shadow-ds-*`, `rounded-ds-*`, `text-ds-<size>`, `leading-ds-*` and `ease-ds-out`.
- Components: `ns_dashboard/ui/src/components/ref12/`, one file each, barrel in `index.ts`.
- Review pages: `#/design` is the component sheet in light and dark; `#/design/ref12` is the whole screen rebuilt with Nightshift copy.

Sections 1–4 below start from Planify's README tables. Sections 5–7 add what the README lacks:

- the depth (z) layer list
- a per-component pixel spec: box in the 1024 frame, padding, radius, fill, border, shadow level, type, icon size
- the conflicts between Planify's values and my own pixel measurements, and which one won

Units:

- **ref px**: pixels in the 1024×768 render. Boxes are `x, y, w, h`.
- **frame px**: the 1440×1080 design frame (ref × 1.406). Planify's token values live at this scale (for example the 40 px greeting, 24 px card radius).

Out of scope: SideRail, AppShell and TopBar. The rail is built on PR #25 and `Shell.tsx` is off limits. The recreation keeps a 140 px slot for the rail and lays its top row out inline.

---

## 1. Tokens (Planify `tokens.css` → our `@theme`)

Tokens come in two layers:

- **Primitives** (`--violet-500`, `--navy-900`, `--gray-*`, `--mint-*`, `--rose-*`, `--amber-400`) are raw values. Components never read them.
- **Semantic** `--ds-*` tokens are what components read. They are re-mapped per theme on `:root, [data-theme="light"]` and `[data-theme="dark"]`. The attribute works on any element, which is how the sheet shows a light and a dark panel side by side.

The existing app tokens (`--bg`, `--card`, `--accent`…) are untouched.

| Group | Tokens → Tailwind | Reference usage |
|---|---|---|
| Backgrounds (back → front) | `bg-canvas` → `bg-app` → `bg-surface` → `bg-sunken` (`bg-ds-canvas`, `bg-ds-app`, `bg-ds-surface`, `bg-ds-sunken`, `bg-ds-hover`) | page → rounded frame → cards → inputs and tiles |
| Accent | `bg-accent` #7466EE, `-hover`, `-soft`, `-subtle` (`bg-ds-accent`…) | rail, selected date, primary buttons, dashed tiles |
| Inverse | `bg-inverse` / `text-inverse` (`bg-ds-inverse`, `text-ds-on-inverse`) | "Add new board", "Find out more" |
| Status | `bg-success` (mint), `text-danger` (rose), `status-attention` (amber), `status-online` | "Package design", "High", bell dot, live dot |
| Text | `text-primary` navy ink, `text-secondary`, `text-muted`, `text-accent` (`text-ds-text`, `-text-2`, `-muted`, `-accent-ink`) | |
| Borders | `border-subtle`, `border-default`, `border-dashed`, `border-focus` | hairlines, search field, dashed tiles, focus |
| Type | `2xs 11` · `xs 12` · `sm 13` · `md 14` · `lg 16` · `xl 20` · `2xl 26` · `display 40` (`text-ds-*`); leading tight 1.2 / snug 1.35 / normal 1.5 | greeting = display/600, card titles = xl/600 |
| Weight | 400 / 500 / 600 / 700 | 600 for titles and buttons, 700 only for ring values and avatar initials |
| Space | 4 px grid `space-1…16` (4 → 64); equals Tailwind's default spacing (`p-6` = 24) | card padding 24, grid gap 20, stack gap 16 |
| Radius | `xs 6` · `sm 8` · `md 10` · `lg 16` · `xl 24` · `2xl 32` · `pill` (`rounded-ds-*`) | buttons md, inner tiles lg, cards xl, promo and frame 2xl |
| Depth | `shadow-0` flat · `shadow-1` resting card · `shadow-2` raised (feature tiles, active task row) · `shadow-3` floating (swiped notification) · `shadow-accent` glow · `shadow-frame` (`shadow-ds-*`, `shadow-ds-focus`) | see §5 |
| Controls | `control-sm 28` · `md 36` · `lg 40` (`h-7` / `h-9` / `h-10`) | buttons, search, segmented |
| Icons | `icon-sm 14` · `md 18` · `lg 22` | |
| Motion | `ease-out` cubic-bezier(0.22, 1, 0.36, 1), `fast 120ms`, `base 200ms` (`ease-ds-out`, `duration-[120ms]`, `duration-200`) | hovers, swipe, toggles |
| Z | `z-raised 2`, `z-floating 10` | elevation 3 cards |
| Illustration | `--ds-illus-paper`, `--ds-illus-accent` (+ `.ds-illus-on-accent`) | spot art fills |
| Font | `--ds-font-sans`: Urbanist → Geist fallback (`font-ds`) | see conflicts |

Dark values come straight from Planify: canvas `#0b0b1c`, app `#12122a`, surface `#1b1b36`, sunken `#23234a`, accent `#8073f3`, inverse `#f3f3fb`. Shadows deepen to black, and the accent glow stays.

Two tokens are additions with no Planify equivalent:

- `--ds-bg-soft-hover`: Planify used the raw `--violet-200` on the soft-button hover.
- `--ds-backdrop-line`: the decorative backdrop curves.

## 2. Primitives (component → reference element)

| Component (file) | Key props | Reference element | Origin |
|---|---|---|---|
| `Icon` | `name`, `size`, `label?` | every icon (lucide registry + Nightshift names) | Planify + draft |
| `Button` | `variant: primary \| inverse \| soft \| ghost \| dashed`, `size`, `leadingIcon`, `trailing` | Accept invite · Add new board · Reschedule · Edit/Share/Clear · Export data + `xls` | Planify |
| `IconButton` | `icon`, `label` (required), `variant: ghost \| soft \| surface \| accent`, `size`, `round`, `dot` | bell, gear, `…`, calendar pager | Planify |
| `Badge` | `tone: accent \| success \| danger \| neutral`, `variant: solid \| soft`, `size: xs \| sm \| md` | High · Package design · Motion design · xls | Planify |
| `StatusDot` | `tone`, `ring`, `label` | green live dot | Planify |
| `Avatar`, `AvatarGroup` | `name`, `src?` / `people`, `max`, `onAdd`, `addLabel` | Rachel Lee, Today-tasks team + "+" | Planify |
| `SegmentedControl` | `options`, `value`, `onChange`, `variant: filled \| plain`, `size` | Light/Dark switch, list/grid/board | Planify |
| `SearchInput`, `TextField` | native input props + `label`, `shortcut` | "Search or type command", quick-reply field | Planify |
| `MetaItem`, `DottedRule` | `icon` | "Sat, 10 May", "11 AM – 11:45 AM", agenda separators | Planify |
| `Card`, `CardHeader` | `elevation 0–3`, `padding`, `radius`, `tone`, `interactive` / `title`, `actions`, `adornment` | every panel | Planify |
| `ProgressBar`, `ProgressRing` | `value`, `tone`, `label` | task progress, 90% / 65% rings | Planify + draft |
| `NavTabs` | `tabs`, `active`, `onChange`, `label` | Dashboard / Workflows / Integrations | Planify |
| `AppWindow`, `PageBackdrop` | — | the rounded frame and the decorated canvas | Draft (frame only, not a shell) |

## 3. Widgets (component → reference block)

| Component (file) | Reference block | Origin |
|---|---|---|
| `Greeting` + `WorkspaceTokens` | "Hi, James! What are your plans for today?" + two round tokens | Planify + draft |
| `CreateTile` (`square` / `row`) | dashed + tile, "Add new assignment" | Planify |
| `FeatureCard` | Stay organized / Sync your notes / Collaborate and share | Planify |
| `NotificationItem` + `SwipeActions` | Upcoming event (raised, tray revealed) and Message card | Planify |
| `AssignmentCard` | packaging-concept assignment | Planify + draft |
| `CalendarCard` = `WeekStrip` + `TimeSlot` + `AgendaItem` | May 2021 agenda | Planify |
| `TaskList` / `TaskRow` | Today tasks table with a raised row | Planify + draft |
| `PromoCard` | Go premium! | Planify |
| `StatCard` | Marketing 90% / Typography 65% | Planify |
| `MeetingCard` | Board meeting invite | Planify |
| `CalendarIllustration`, `NotesIllustration`, `CollaborateIllustration`, `GiftIllustration` | the four reference spots | Planify |
| `StoryIllustration`, `ClipsIllustration`, `SeriesIllustration`, `VoiceIllustration` | Nightshift spots in the same system | Draft, redrawn on Planify's Spot |

### Which one won, and why
- **Planify** wins wherever both existed: tokens, Button, IconButton, Badge, StatusDot, Avatar/AvatarGroup, SegmentedControl, SearchInput, TextField, MetaItem, DottedRule, Card/CardHeader, NavTabs, CreateTile, FeatureCard, NotificationItem/SwipeActions, CalendarCard family, PromoCard, StatCard, MeetingCard, and the four reference illustrations.
  - The owner approved Planify.
  - Its API is more composable: elevation levels instead of one-off shadows, generic NotificationItem + SwipeActions instead of my single FloatingEventCard and MessageRow, and Badge in place of my Chip and KbdChip.
  - It has real keyboard behaviour (the SegmentedControl uses roving tabindex with arrow keys).
  - Its dark theme is designed rather than derived.
- **Planify + draft**: Planify's look and API, with fixes from my draft.
  - `ProgressBar`/`ProgressRing`: fills animate `transform` / `stroke-dashoffset` instead of `width`.
  - `Greeting`, `WorkspaceTokens`, `AssignmentCard`, `MeetingCard`, `CalendarCard`, `SearchInput`, `AvatarGroup`, `NotificationItem`: every visible string and aria label is a prop. Planify hard-coded "Hi, {name}!", the High/Medium/Low labels, "Reschedule"/"Accept invite", "Previous week", "Add member" and "More options".
  - `TaskRow`: generic metric and meta columns instead of fixed Duration/attachments/due; wraps by container width rather than a 560 px media query.
  - `Greeting` gains `size="lg"` (32 px) for salutations longer than "Hi, James!".
  - `Icon` registry gains the Nightshift icons.
- **Draft only**: `AppWindow` and `PageBackdrop`. Planify's frame lives in `AppShell`, which is out of scope. The four Nightshift illustrations were redrawn on Planify's `Spot` system (currentColor ink, `--ds-illus-*` fills) so they theme like the originals.
- **Deleted drafts** (replaced): ActionButton, KbdChip, IconButton/Kebab, SegmentedToggle, CommandSearch, ExportButton, TopNav/TopBar, AvatarStack, HeroGreeting, DashedAddCard, FeatureCard, Card, Chip, NotificationsCard, FloatingEventCard, MessageRow, AssignmentsCard, CalendarCard, AgendaTimeline, TodayTasksCard, TaskRow, ProgressBar, RingProgress, PremiumCard, StatRingCard, BoardMeetingCard and the `--r12-*` tokens.

## 4. Dashboard layout (Planify `Dashboard.css`)

A 6-column grid with named areas and a 20 px gap (`.ds-dash` in `styles.css`):

```
greeting greeting create f1     f2   f3
notif    notif    assign assign cal  cal
tasks    tasks    tasks  promo  side side     (side = 2 stat cards + meeting)
```

| Viewport (Planify) | Container (ours) | Columns | Areas |
|---|---|---|---|
| ≥ 1280 px | ≥ 1020 px content | 6 | as above |
| 860–1279 px | 600–1019 px | 4 | `greeting×3 create` / `f1 f2 f3 f3` / `notif×2 assign×2` / `cal×2 side×2` / `tasks×3 promo` |
| < 860 px | < 600 px | 1 | greeting, create, f1, f2, f3, notif, assign, cal, tasks, promo, side |

Planify switches on viewport width. We switch on **container** width, so the same grid reflows inside the scaled review frame and inside any page shell. The content widths correspond to Planify's viewport breakpoints minus its canvas padding and rail.

Grid behaviour:
- Hero cards stretch to the row height.
- The greeting is vertically centred in its area.
- The `side` area is its own 2-column grid with 16 px gaps, and the meeting card spans both columns.

## 5. Depth (z) layers

| z | Layer | Fill | Shadow token | Notes |
|---|---|---|---|---|
| 0 | `PageBackdrop` (canvas) | `bg-canvas` #E7E7F1 | none | hairline white curves at ~70%, dots: mint 7 px (22, 586 ref), mint 4 px (935, 22), violet 9 px (990, 511); outline ring 25 px (55, 720), slate disc 20 px (918, 725) |
| 1 | `AppWindow` (frame) | `bg-app` #F7F7FB | `shadow-frame` `0 50 100 -40 rgba(40,36,110,.28)` | radius 2xl 32; box ref 44, 34, 936, 699 |
| 2 | Side rail (PR #25) | `bg-accent` | — | ref x 44–112, wave y ≈ 160–555 |
| 3 | Body cards, flat | `bg-surface` | `shadow-1` | radius xl 24 |
| 3 | Inset tiles: CreateTile, TextField, agenda icon tile, swipe tray | `accent-subtle` / `sunken` | `shadow-0` or none | dashed `border-dashed` 1.5 px on CreateTile |
| 3 | PromoCard | `bg-accent` | `shadow-accent` | the only saturated surface |
| 4 | Raised: FeatureCards, highlighted TaskRow, NotificationItem resting | `bg-surface` | `shadow-2` (rows resting: hairline + `shadow-1`) | the 3rd feature tile crosses the frame edge by 12 ref px |
| 5 | Floating: swiped NotificationItem | `bg-surface` | `shadow-3`, `z-floating 10` | slides 52 px left over the sunken tray, crossing the card edge |
| 6 | Glows and badges on top: selected day (4 px soft halo + accent glow), active segment, plus tiles | `bg-accent` | `shadow-accent` | bell dot has a 2 px `bg-app` ring |

## 6. Per-component pixel spec

Box = ref px in the 1024 frame (x, y, w, h). Values in the other columns are frame px, i.e. the token values the code uses.

| Component | Box (ref) | Padding | Radius | Fill / border | Shadow | Type | Icon |
|---|---|---|---|---|---|---|---|
| NavTabs | 144, 64, 230, 22 | 8 px vertical per tab, 24 between | xs 6 (focus) | ink text, inactive 78% | — | sm 13 / 600 | 15 |
| SearchInput | 387, 62, 150, 24 | 0 12 | md 10 | surface, `border-default` 1 px; focus `border-focus` + ring | — | xs 12 | 15 |
| SegmentedControl (theme) | 631, 62, 84, 24 | track 3 | md 10, items sm 8 | track `accent-soft`; active `accent` | active `shadow-accent` | xs 12 / 500, items 30 tall | 14 |
| IconButton bell / gear | 726, 62 / 752, 62, 24, 24 | — | sm 8 | ghost | — | — | 18; dot 7 amber |
| Button soft "Export data" + Badge xs | 779, 62, 81, 24 | 0 16 | md 10; badge pill | `accent-soft`; badge `accent` | — | sm 13 / 500; badge 10 / 600 | 15 |
| Button inverse "Add new board" | 870, 62, 80, 24 | 0 16, h 36 | md 10 | `bg-inverse` navy | — | sm 13 / 600 | — |
| Greeting | 144, 115, 256, 130 | — | — | — | — | display 40 / 600, leading 1.2, −0.02em; desc md 14 / 1.5 secondary, max 34ch, 20 below | — |
| WorkspaceTokens | 278, 120, 42, 18 | — | full | `accent-soft`, 3 px `bg-app` halo, −6 overlap | — | — | 14 / 2.2 |
| CreateTile square | 431, 114, 130, 131 | — | xl 24 | `accent-subtle`, 1.5 dashed `border-dashed` | plus tile `shadow-accent` | — | plus 14 in 26 tile (sm 8) |
| FeatureCard ×3 | 575 / 718 / 862, 114, 130, 132 | 16 20 | xl 24 | surface | `shadow-2` | title md 14 / 600; desc 2xs 11 muted, 2 lines | art ≤ 120 w |
| Card (Notifications) | 145, 278, 255, 204 | 24 | xl 24 | surface | `shadow-1` | header xl 20 / 600, 20 below | — |
| NotificationItem raised | 131, 326, 218, 65 | 12 12 12 16 | lg 16 | surface | `shadow-3` | title md 14 / 600 + 7 px dot; subtitle 2xs muted; meta 2xs ink, 24 apart | meta 13; `…` 14 in 24 |
| SwipeActions tray | 145, 326, 237, 65 | right 12 | lg 16 | `bg-sunken` | — | — | 14 in 24, 12 apart |
| NotificationItem resting + TextField | 165, 401, 215, 62 | 12 12 12 16; field 0 12 | lg 16; field sm 8 | surface + `border-subtle` ring; field `accent-subtle` | `shadow-1` | title md 14 / 600; field 2xs 11, h 30 | 13 |
| AssignmentCard | 434, 318, 216, 92 | 12 12 16 16 | lg 16 | surface + `border-subtle` ring | — | discipline Badge sm (2xs / 600); title md 14 / 600 snug; priority Badge md (xs / 600, h 28); tag Badge solid success md; assignee 2xs secondary | `…` 14; avatar 18 |
| CreateTile row | 434, 424, 216, 39 | — | lg 16 | `accent-subtle`, 1.5 dashed | plus `shadow-accent` | xs 12 / 500 | plus 14 in 22 (xs 6) |
| CalendarCard | 685, 278, 260, 215 | 24 | xl 24 | surface | `shadow-1` | month xl 20 / 600; pager soft round 24 | 14 |
| WeekStrip | 702, 325, 228, 30 | gap 4, 8 between weekday and date | date full | selected `accent` + 4 px `accent-soft` halo | `shadow-accent` | weekday 2xs muted; date sm 13 / 600 tabular, 30 circle | — |
| TimeSlot + AgendaItem | 702, 366–475 | item 8 0, gaps 12 | tile md 10 | tile `sunken`; rule 2 px dotted `border-default` | — | time md 14 / 600; title sm 13 / 600; meta 2xs muted + 5 px dot | 16 in 34 tile; `⋮` 14 in 24 |
| Card (Today tasks) + AvatarGroup + SegmentedControl plain | 145, 499, 380, 203 | 24 | xl 24 | surface | `shadow-1` | title xl 20 / 600 | avatars 24, −7 overlap, 2 px ring; view items 26 round |
| TaskRow | 153, 556, 345, 38 (raised) | 12 16 12 8 (raised 12 16 12 16, −8 left) | lg 16 | raised: surface; hover: `bg-hover` | raised `shadow-2` | title sm 13 / 600; date 2xs muted; metric label 2xs muted, value sm 13 / 600; % xs 12 / 600 | meta 13; track 4 × ≤ 56 |
| PromoCard | 541, 496, 129, 206 | 24 20 | 2xl 32 | `bg-accent` | `shadow-accent` | title 2xl 26 / 600 (xl at narrow); desc xs 12 at 88%; CTA inverse lg h 40 navy | art ≤ 120 |
| StatCard ×2 | 685 / 822, 510, 125, 82 | 16 20 | xl 24 | surface | `shadow-1` | eyebrow 2xs / 600 uppercase tone; title md 14 / 600; captions 2xs muted | ring 48 / 4, value sm 13 / 700; button sm h 28 |
| MeetingCard | 685, 608, 262, 94 | 16 20 | xl 24 | surface | `shadow-1` | title xl 20 / 600; when 2xs muted + 8 px accent dot; desc xs 12 snug, max 24ch; buttons md h 36 soft + primary | Edit 13 |

## 7. Conflicts: Planify vs my measurements (Planify wins)

| Item | Planify | My measurement | Note |
|---|---|---|---|
| Accent | `#7466EE` | `#6F6BF0` (sampled) | ΔE small; Planify's ramp is coherent (50–700) |
| Ink | `#15153B` | `#101339` | |
| Canvas | `#E7E7F1` | `#CED0E2` sampled | the render's canvas is darker; Planify lightened it, and the recreation follows Planify |
| Frame | `#F7F7FB` | `#F7F8FB` | same |
| Success / danger | mint `#5FBD97` / rose `#E14B66` | `#5DAC92` / ring `#A3172F` | the reference's typography ring is a deeper crimson than Planify's rose |
| Card radius | xl 24 | 28 (20 ref × 1.406) | |
| Greeting | display 40 | 36 | Urbanist runs narrower than Geist; the recreation uses `size="lg"` 32 for the longer Nightshift salutation |
| Card padding | 24 (md cards 16/20) | 24 | same |
| Body gutter | 20 | 22 | |
| Calendar day circle | 30 | 28 | |
| Agenda icon tile | 34, radius md | 32, radius 8 | |
| Ring | 48 / 4, value 13 / 700 | 48 / 4, 12 / 600 | |
| Smallest text | 2xs 11 (eyebrow 10) | 11–12 minimum | eyebrow raised to 11 (2xs) for legibility; it stays uppercase as in Planify and the reference |
| Swipe offset | 52 px left | 44 px past the list edge | |
| Font | Urbanist (Google Fonts) | — | the dashboard must work offline and Urbanist is not bundled, so text renders in Geist. One install (`@fontsource-variable/urbanist`) makes Urbanist live; the token already lists it first |

## 8. Interaction and motion
- Hover lift on interactive cards: −2 px and `shadow-2`, 200 ms `ease-out`. TaskRow hover = `bg-hover`. Highlighted row = surface + `shadow-2`.
- Swipe to reveal: the content translates −52 px and the tray fades in (200 ms). Tray buttons leave the tab order when it is closed.
- SegmentedControl: active fill + accent glow; arrow keys move and select; roving tabindex.
- WeekStrip: hover = `accent-soft` circle; selected = accent + halo + glow.
- Progress fills tween via `transform`; rings via `stroke-dashoffset` (600 ms).
- Buttons press 1 px down. Focus = `--ds-focus-ring` (3 px accent at 35%). Reduced motion turns transitions off inside `.ds-root`.

---

## Applying to Nightshift pages

Not implemented in this PR. Each page keeps its purpose from `BUILD_SPEC.md` §20.2. The frame and rail (PR #25) wrap all pages, and the body uses the `.ds-dash` container grid (6 → 4 → 1 columns) with page-specific area maps.

| Page | ref12 components | Grid areas (6-col) |
|---|---|---|
| **Queue** (home) | Greeting (tonight's count), FeatureCard-sized media tiles for the ready filmstrip, AssignmentCard rows as the posting checklist, CreateTile row for "Paste the YouTube link", Button primary/soft for Approve/Reject, Badge xs keycaps for A / R / → | `player×4 checklist×2` / `strip×6` |
| **Create** | CreateTile + FeatureCards for the two choices (story video / clips from my video), TaskList for jobs with progress, NotificationItem list for "Earlier" with plain failure reasons | `greeting×2 create f1 f2 f3` / `jobs×4 earlier×2` |
| **Library** | NavTabs for Ideas / Stories / Voices / Formats / Footage, AssignmentCard for ideas (Badge statuses), FeatureCard tiles for formats, PromoCard "Design or clone a voice" on Voices | `tabs×6` / cards in 3 × 2-col areas |
| **Assistant** | NotificationItem-style thread, raised NotificationItem as the confirm card, SearchInput-style composer | `chat×4 context×2` |
| **Channels** | Card per channel with AvatarGroup, CalendarCard for schedule, SegmentedControl for Pause/Resume, Badge pools | `channels×4 cal×2` |
| **Results** | StatCards (QC pass rate, retention target), TaskList-style ranked rows with ProgressBar, MeetingCard as the go/no-go milestone, CreateTile square as the CSV drop zone | `stats×2 stats×2 drop×2` / `lists×4 milestone×2` |
| **Settings** | Cards with Buttons, SegmentedControl (theme, notifications), WeekStrip for run time, Button soft + Badge for diagnostics export | `settings×4 update×2` |

Notes:
- The engine status pill from today's Strip moves into the top row's action group.
- Keep at most one PromoCard-style accent surface per page.
- Mobile: the 1-column order follows the area map, and every component is checked at 390 px on `#/design`.
