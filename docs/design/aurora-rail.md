# Aurora Rail — A.S.H.R.A design system

This is the design language the viewer uses across the Home, Galaxy, and Brain
surfaces. It is a dark, editorial "command deck": a persistent left rail, an
aurora of violet and cyan light behind quiet cards, and a single coral accent
reserved for anything that needs a decision.

The stylesheet in `viewer/chrome.css` owns the tokens, the rail, and small
shared utilities. Each surface adds its own layout in `home.css`,
`styles.css`, or `brain.css`.

## Principles

1. **Quiet by default, loud on purpose.** Surfaces are near-black and
   low-contrast; glow and colour are spent on the brief, the selected node, and
   the one thing waiting for approval.
2. **Local is green, action is coral.** Lime means the local lane and healthy
   memory. Coral means an approval, an overdue item, or something that will
   leave the machine. Never use coral for decoration.
3. **One rail, one product.** The rail, brand, lane card, and owner block are
   identical on every surface so moving between Home, Galaxy, and Brain feels
   like switching rooms in the same building.
4. **Editorial before dashboard.** Large Sora headlines, an italic Instrument
   Serif accent, and 10.5 px mono kickers carry the hierarchy. Cards are for
   evidence, not chrome.
5. **Text tells the truth.** The lane card reports the configured provider,
   not a decorative claim. Empty states say what is missing and what to do.

## Tokens

Defined in `viewer/chrome.css` under `:root`.

| Token | Value | Use |
|---|---|---|
| `--bg` | `#07060f` | Page and stage background |
| `--rail` | `#0a0815` | Left rail |
| `--card` | `#100d1e` | Cards |
| `--card-2` | `#151129` | Raised rows and inputs |
| `--ink` | `#f6f2ff` | Primary text |
| `--ink-2` | `#d5cfea` | Secondary text |
| `--muted` | `#a8a1c6` | Body copy |
| `--faint` | `#847ca4` | Kickers, meta, timestamps |
| `--line` | `rgba(167,139,250,.15)` | Hairlines and card borders |
| `--line-2` | `rgba(167,139,250,.28)` | Hover borders, emphasis |
| `--violet` | `#a78bfa` | Primary accent |
| `--indigo` | `#7c6bff` | Primary buttons and glows |
| `--cyan` | `#3ddcff` | Notes, live status, secondary accent |
| `--pink` | `#ff7ac6` | Memories |
| `--coral` | `#ff6a57` | Approvals, errors, anything destructive |
| `--lime` | `#b6f36a` | Local lane, healthy memory, people |
| `--amber` | `#ffc861` | Cloud lane, expiring connectors |

Memory kinds have their own scale (`--fact`, `--preference`, `--person`,
`--project`, `--event`, `--task`, `--document`, `--procedure`,
`--observation`) used by the Brain and the capture chips.

## Typography

| Role | Family | Size / weight |
|---|---|---|
| Hero and page titles | Sora | 32–44 px, 300, tight tracking |
| Emphasis inside a title | Instrument Serif italic | 1.08 em, 400 |
| Card titles | Sora | 21–23 px, 400 |
| Body | Inter | 14–16.5 px, 400 |
| Kicker / label | JetBrains Mono | 9–10.5 px, 500, 0.11–0.2 em, uppercase |
| Numerals | Sora | 23 px, 600 in metric blocks |

Uppercase mono is for labels only. Never set a sentence in it. Keep body copy
at 1.5–1.6 line-height.

## Shell

```
┌───────────────┬──────────────────────────────────────────┐
│ brand         │  kicker                                  │
│ Surfaces      │  Hero headline                           │
│  Home         │  ┌──────────────────┐  ┌───────────────┐ │
│  Galaxy       │  │ primary card     │  │ side card     │ │
│  Brain        │  └──────────────────┘  └───────────────┘ │
│  Desk (soon)  │                                          │
│               │                                          │
│ lane card     │                                          │
│               │                                          │
│ owner         │                                          │
└───────────────┴──────────────────────────────────────────┘
```

- Rail is 272 px on desktop, sticky, and folds into a horizontal top bar at
  820 px.
- `.rail-card` shows the model lane: lime pulse and "Local lane" for local
  providers, amber pulse and "Cloud lane" for hosted providers, neutral for an
  unconfigured model. The track and footer carry real memory counts.
- The owner block is pinned to the bottom of the rail.

## Components

### Cards
18 px radius, 1 px `--line` border, 22–24 px padding, `--card` fill. Only the
daily brief gets the aurora gradient and the `✦` watermark. Cards may use
container queries (`container-name: card`) to restack their own controls.

### Kicker + title
Every card starts with a mono `kick` and a Sora `h2`. The `head` row aligns
the title left and one action or count right.

### Buttons
- Primary `.btn.go`: violet-to-indigo gradient, ink text, soft glow.
- Secondary `.btn`: translucent fill with `--line-2`.
- Destructive: coral text/border on a coral-tinted fill.
- Quiet links: violet text, no border.

### Pills and chips
Used for counts, states, and filters. Mono, uppercase, 9.5 px, 5–10 px
padding, 6 px radius. Coral for approvals, cyan for review, lime for healthy,
amber for expiring.

### Needs You
One card per decision: approval, memory proposal, or connector warning. The
kicker states the type; the body states the claim; the actions are explicit
verbs. Approvals are coral; proposals are cyan; warnings are amber. A count
badge sits in the card header, lime at zero.

### Lane card
The rail's status card. It is rendered by `chrome.js` from `GET /status`, so it
is identical on every surface. The footer line carries surface-specific counts
(memories on Home and Brain, notes and links on Galaxy).

### Capture kinds
Quick capture uses a radiogroup of kind pills (Fact, Preference, Person, …)
instead of a select. Arrow keys move the selection, the active pill takes the
kind's colour dot, and the chosen kind is sent with the proposal.

### Command palette
Home binds `⌘K` / `Ctrl+K` to a centred glass palette with a command list.
Commands are verbs, not nouns: Ask, Capture, Open the Brain, Ingest, Rebuild.

### Empty states
A glyph, a short title, and one sentence of instruction. Never a blank card and
never an apology.

## Surfaces

### Home
The command deck. The brief leads; Needs You and Connectors sit in the right
column; Quick capture, Recently remembered, and Recent conversations form the
lower row. The activity card records what the system just did. Time is shown
in the 24-hour clock with a mono date.

### Galaxy
Full-bleed 3D graph with a floating title, constellation legend, note panel,
and a bottom ask dock. The graph uses group colours; the selected node gets a
white ring and a label. Answers cite note chips that fly the camera. A quick
find bar searches notes, memories, and people; Notes / Memories / People filter
the results, a note result focuses the graph, and a memory or person result
opens the Brain at that record (`brain.html?memory=<id>`).

### Brain
The trust surface. A three-part filter column (census, lifecycle, health), a
workspace with Graph / Timeline / Library tabs, and a right-hand detail panel
with provenance, confidence, related records, and lifecycle actions. Forgetting
requires a second click. `?memory=<id>` opens that record's panel directly, and
lists render skeleton rows while they load.

## Interaction and motion

- Easing is `cubic-bezier(.16,1,.3,1)`; durations are 180–420 ms.
- Focus is always visible: 1 px `#b9a7ff` outline with 3 px offset.
- Validation and errors use coral; success uses lime.
- `prefers-reduced-motion` disables animation and transition duration.
- Keyboard: `⌘K`/`Ctrl+K` palette on Home, `/` focus search on Galaxy, `Esc`
  closes panels and palettes, `R` resets the Galaxy view.

## Responsive rules

| Width | Behaviour |
|---|---|
| > 980 px | Two-column layouts, full rail, legend visible |
| 820–980 px | Rail stays, secondary columns tighten, legend hides |
| ≤ 820 px | Rail becomes a top bar; columns stack; dock stays reachable |
| ≤ 640 px | Hero and controls scale down; non-essential buttons hide |

Brain's filter column becomes a band above the workspace below 820 px, wrapping
so every control stays visible; below a 480 px container it becomes one
swipeable strip with a faded trailing edge.

Cards use container queries where the card can become narrow inside a wide
page. Test every surface at 1440, 840, and 390 px.

## Accessibility

- Landmarks: `aside.rail`, `main`, `nav`, `section` with labelled headings.
- Every control is a real `button`, `a`, `input`, or `select`; icon-only
  controls carry `aria-label`.
- Live regions (`aria-live="polite"`) announce status, toasts, and queue
  changes.
- Colour is never the only signal: approvals also say "Needs you", the lane
  card also names the provider.
- The 3D stage is `aria-hidden`; every note is reachable through the panel,
  legend, and search.
