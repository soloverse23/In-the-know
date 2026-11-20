# In The Know: brand

**Promise:** the memory and analyst of your business, inside Claude. You tell it what happens, and it keeps the numbers exact.

**Voice:** a calm, precise business assistant. It uses short sentences, real names and real numbers. It never shows hype and never invents anything. In the owner's language it sounds like a trusted bookkeeper who also knows the customers.

## Mark
An ink rounded square with a light arc (the "knowing look") and a signal-coral dot (the insight).

```
<svg viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#14161F"/>
<path d="M8.5 20.5c2.6-6.2 12.4-6.2 15 0" stroke="#FAF8F4" stroke-width="2.6" fill="none" stroke-linecap="round"/>
<circle cx="16" cy="12.4" r="3.3" fill="#FF6B4A"/></svg>
```

`packaging/icon.png` is the 512 px version of the mark. The same mark is the connector icon in Claude, sent through the MCP `icons` field.

## Colours
| Token | Hex | Use |
|---|---|---|
| Ink | `#14161F` | mark, hero background |
| Paper | `#FAF8F4` | mark arc, hero text |
| Signal | `#FF6B4A` | the insight dot, hero glow |
| Mint | `#16B98A` | live/working pulse |

The owner's own brand colours (`brand.primary`, `brand.accent`) colour the content: KPIs, charts and buttons. The In The Know signature stays in the mark and the footer.

## Type
- **Inter:** all UI text, with tabular numbers.
- **Instrument Serif:** the welcome hero only.
- The owner can switch the content font. The allowed fonts are Inter, Manrope, DM Sans, Nunito, Space Grotesk, Fraunces, Playfair Display and IBM Plex Sans.

## Card anatomy
- **Header:** the mark (or the owner's logo), the title, a subtitle, and a "Live" pill while a tool is working.
- **Body:** blocks (KPI tiles, alerts, charts, tables with row actions, the customer profile, the live capture feed, import cards, the checklist, the schema, questions).
- **Footer:** the small mark and "In The Know · memory of {business}". The owner can hide it.
- The theme follows Claude's light or dark mode; the owner can force one. Layouts work down to phone width.

## Screenshots
See `docs/screenshots/` for onboarding, import, the live capture card, the brief (ro, fr, de; light, dark, mobile), the customer file, this week's list, design, a custom dashboard and the data view.
