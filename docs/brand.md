# In The Know: brand

**Promise:** the memory and analyst of your business, inside Claude. You tell it what happens, and it keeps the numbers exact.

**Voice:** a calm, precise business assistant. It uses short sentences, real names and real numbers. It never shows hype and never invents anything. In the owner's language it sounds like a trusted bookkeeper who also knows the customers.

## Mark
A soft paper rounded square with the lowercase wordmark "itk", set in Inter Bold and converted to outlines (so it looks the same where Inter is not installed). The letters are ink; the only colour is the dot of the i, in signal indigo: the insight.

```
<svg viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#F5F3EE"/><path d="M6.52 22.37V13.24H9.05V22.37ZM15.59 13.24V15.14H13.87V19.57Q13.87 19.92 13.98 20.11Q14.09 20.3 14.28 20.38Q14.48 20.46 14.73 20.46Q14.91 20.46 15.09 20.42Q15.27 20.39 15.36 20.37L15.76 22.26Q15.57 22.32 15.22 22.4Q14.88 22.48 14.39 22.5Q13.47 22.53 12.78 22.25Q12.1 21.97 11.72 21.38Q11.34 20.8 11.34 19.9V15.14H10.09V13.24H11.34V11.05H13.87V13.24ZM16.9 22.37V10.2H19.44V16.71H19.57L22.5 13.24H25.41L22.02 17.19L25.58 22.37H22.61L20.12 18.69L19.44 19.47V22.37Z" fill="#14161F"/><path d="M7.79 12.06Q7.22 12.06 6.82 11.68Q6.42 11.31 6.42 10.78Q6.42 10.26 6.82 9.88Q7.22 9.5 7.79 9.5Q8.35 9.5 8.75 9.88Q9.16 10.26 9.16 10.78Q9.16 11.31 8.75 11.68Q8.35 12.06 7.79 12.06Z" fill="#4F46E5"/></svg>
```

`packaging/icon.png` is the 512 px version of the mark. The same mark is the connector icon in Claude, sent through the MCP `icons` field.

## Colours
| Token | Hex | Use |
|---|---|---|
| Ink | `#14161F` | mark letters, text |
| Paper | `#FAF8F4` / `#F5F3EE` | page background / mark tile |
| Signal | `#4F46E5` | the i's dot, accents, buttons, hero glow |
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
