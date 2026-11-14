# Design

The Canvas page is a **diagnostics panel**, not a product surface. Its reader is
the person running the proxy, usually mid-problem, often at a narrow width
inside a chat panel. Its job: say whether the chain is working, and when it is
not, say exactly which link failed and why.

Words for it: **precise, quiet, instrument-like.** Not friendly, not branded.
It should read like the readout on a piece of equipment that someone designed
on purpose.

## What was wrong before

The old page wasn't slop by accident — it hit the default kit exactly:

- five identical rounded metric cards, one radius, one border, no hierarchy
- a glowing green dot as the only signal of health, which is both the most
  common AI dashboard tell and a weak signal: colour alone, and the absence of
  a problem looked like the presence of a feature
- uppercase 11px labels tracked out across everything
- a `A · B · C` meta line in the footer explaining the architecture to a reader
  who did not ask
- a purple gradient app icon

## Palette

No green. "Nothing is wrong" should be quiet, and a problem should be the only
thing that raises its voice. Health is shown by a warm accent, not a celebratory
colour.

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0d0f11` | page |
| `--surface` | `#15181b` | panels |
| `--surface-2` | `#1a1e22` | nested / hover |
| `--line` | `#262b30` | rules |
| `--text` | `#e8ecef` | primary |
| `--muted` | `#98a2a9` | secondary, 6.9:1 on bg |
| `--faint` | `#6d777e` | tertiary, 4.6:1 on bg |
| `--accent` | `#e0a458` | warm sand — links, live state |
| `--warn` | `#d9a441` | degraded |
| `--danger` | `#e0685c` | failed |

## Type

One superfamily. IBM Plex Sans for text, IBM Plex Mono for anything that is
data — model names, numbers, JSON. Plex has a humanist quirk (the double-storey
`a`, the flat-tailed `l`) that keeps it from reading as another Inter clone,
and the sans/mono pair share proportions, so mixed lines sit level.

Scale: 12 / 13 / 15 / 18 / 26. Sentence case everywhere; no tracked-out caps.

Loaded from Google Fonts with a local fallback chain. If the sandbox blocks the
request the page falls back to the system sans, which is a graceful loss, not a
broken layout.

## Layout

A single left-aligned column, 820px max. Sections separated by space and one
hairline, not by a card per idea.

```text
┌──────────────────────────────────────────────────────┐
│ ◇ Gemini Canvas Proxy                                │
│ Ready                          written to :8765      │
├──────────────────────────────────────────────────────┤
│  CANVAS PAGE ◇──────◇ EXTENSION ◇──────◇ NATIVE HOST │
│  running            relaying            connected    │
├──────────────────────────────────────────────────────┤
│  12 requests   11 answered   1 failed   1.24s median │
│  1.2k in   340 out   1.5k total                      │
├──────────────────────────────────────────────────────┤
│  12:18:58  gemini-3-flash-preview   200   1.2s  ▲    │
│    ├ request  POST /v1beta/models/...                │
│    │          { "contents": [ … ] }                  │
│    └ usage    in 7 · out 2 · total 76                │
└──────────────────────────────────────────────────────┘
```

## Signature move

The **signal path**: three diamonds joined by a hairline that thickens and turns
accent when both ends are up. It is the one deliberate graphic element, it
encodes the actual topology (three links in series), and it replaces the row of
status pills the old page had. Diamonds rather than dots because a filled
circle is the generic health indicator and this needs to look like a schematic.

## Interaction

- Activity rows expand in place to show the request that was sent, the usage
  split, and — for speech models — an audio player. One row open at a time.
- Errors show the message and the error payload, not a red dot.
- Nothing animates except the connecting segment while a handshake is in
  flight, and that respects `prefers-reduced-motion`.

## Rules this page follows

- No colour-only signals: every state has a word.
- No decoration that does not carry information.
- No explanatory copy aimed at the author. If a line does not help the reader
  act, it goes.
- Focus is visible and keyboard paths work; the expander is a real `<button>`.
