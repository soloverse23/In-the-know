# In The Know: brand

**Promise:** the memory and analyst of your business, inside Claude. You tell it what happens, and it keeps the numbers exact.

**Voice:** a calm, precise business assistant. It uses short sentences, real names and real numbers. It never shows hype and never invents anything. In the owner's language it sounds like a trusted bookkeeper who also knows the customers.

## Mark
A light paper rounded square (with a faint ink hairline) with "ITK" drawn as one minimal monoline wordmark. The letters are joined: the signal-coral T's crossbar runs from the top of the I to the top of the K, so the three letters read as one connected shape.

```
<svg viewBox="0 0 32 32"><rect x=".5" y=".5" width="31" height="31" rx="8.5" fill="#FAF8F4" stroke="#14161F" stroke-opacity=".12"/>
<path d="M6.5 10V22M20.5 10V22M20.5 16L25.5 10M20.5 16L25.5 22" fill="none" stroke="#14161F" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M6.5 10H20.5M13.5 10V22" fill="none" stroke="#FF6B4A" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
```

`packaging/icon.png` is the 512 px version of the mark. The same mark is the connector icon in Claude, sent through the MCP `icons` field.

## Colours
| Token | Hex | Use |
|---|---|---|
| Ink | `#14161F` | mark I and K, hero background |
| Paper | `#FAF8F4` | mark background, hero text |
| Signal | `#FF6B4A` | mark T (the joining stroke), hero glow |
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
