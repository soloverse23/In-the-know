# Plan: People-first packs (real estate agents, coaches, tour leaders)

Status: P1 (people layer) implemented · P2–P8 proposed · 2026-11-19

## 1. Goal

In The Know today understands a *business*: transactions, costs and metrics. Real estate agents, sports coaches and tour leaders need it to understand *people*: what each person wants, cannot do, is sensitive about, who they decide with, and how that changes over time.

This plan adds three things:

1. A people layer inside the flexible schema engine that every trade can shape.
2. Three new packs built on that layer.
3. A smooth way to bring in years of existing data, all at once or a little at a time, and to keep it fed from other connectors.

The existing principles stay:
- Quiet by default, with cards only on request.
- Exact numbers and matching computed in code, not by the model.
- Every change can be previewed and undone.
- Nothing is ever guessed silently.

---

## 2. What each trade needs (requirements matrix)

| Need | Real estate | Coach | Tour leader | How the engine covers it |
|---|---|---|---|---|
| Rich person profile (preferences, limits, goals, style) | buyer/seller wishes, deal-breakers | goals, injuries, motivation style | diet, allergies, mobility, interests | **Fact store** (§3.1) |
| Structured criteria with ranges | budget 3–4bn, 80–120 m², areas | target weight, pace | budget tier | **`range` and `tags` field types** (§3.2) |
| Household / group links | spouse decides, referred-by | team, training partner | roommates, who conflicts with whom, family groups | **Relations** (§3.3) |
| Dated events with several people | viewings | sessions, classes | tour departures, daily activities | **`occasion` role and participants** (§3.4) |
| Values over time | price changes, offers | weight, 1RM, run times, sleep, pain | — | **`observation` role** (§3.5) |
| Unique things to match people to | listings | programs | tours / rooms | **`item` role and match specs** (§3.6) |
| Warnings | lead going cold, stalled deal | pain repeated, adherence drop, package ending | passport expires before the trip, allergy at tomorrow's restaurant | **Watch rules** (§3.7) |
| Sensitive data | finances, divorce | health | health, passports | **Sensitivity and consent** (§3.8) |

The design rule is that **no new concept is trade-specific**. A pack is only a configuration of these generic pieces: entities, fact kinds, relation types, match specs and watch rules. The same pieces then work for a groomer, a wedding planner or a tutor later.

---

## 3. Flexible database: changes to the engine

### 3.1 Fact store (new system table `facts`)

Structured columns cannot hold everything we learn about a person. Facts are typed, sourced, dated knowledge attached to any record (usually a party).

```
facts(
  id, entity, record_id,            -- who/what the fact is about (any entity, usually party)
  kind,                             -- from the pack's fact vocabulary: preference, dealbreaker, goal,
                                    --   constraint, health, motivation, style, family, document, sensitivity, ...
  key, value, value_json,           -- e.g. key='floor', value='not ground floor'
  polarity,                         -- likes | dislikes | must | must_not | neutral
  sensitivity,                      -- normal | private | special (health, finances, documents)
  confidence,                       -- confirmed | stated | inferred
  source, quote, observed_at,       -- provenance: chat/file/email id, exact words, when it was said
  valid_until, superseded_by,       -- facts change; old ones are kept, not overwritten
  _created
)
```

- Packs declare **fact kinds**, with labels in 4 languages, synonyms and a default sensitivity. Claude can add kinds at runtime through `change_schema` (`add_fact_kind`).
- **Promotion:** a fact that matters for matching or metrics can be promoted to a real field. For example, "budget" mentioned in 30 profiles is offered as a `range` field on `buyer`, using the existing "convert field" flow with preview and undo.
- **Contradiction check** on write. A new fact with the same `(record, kind, key)` and a different value does not overwrite the old one. It raises a question: "In March Sara said 3bn max, today 4bn. Update?" The confirmed fact supersedes the old one.
- `search_knowledge` and `find_records` can filter by facts ("all buyers who must have parking").

### 3.2 New field types

| Type | Stores | Used for |
|---|---|---|
| `range` (number or money) | `{min, max}` in two columns | budget, size, rooms, target pace |
| `tags` | set of enum values (join table) | areas, interests, diet, equipment |
| `duration` | minutes | session length, walking time |
| `geo` | lat/lng and label | listing location, meeting point |
| `file` | path or URI and type | passport scan, contract, floor plan |

Each type gets normalizers in all 4 languages: "3 to 4 milliards", "între 80 și 100 mp", "vegan, keine Nüsse".

Any field can also carry `sensitive: true` and `expires: true`. A date field with `expires` is watched automatically.

### 3.3 Relations (new system table `relations`)

```
relations(id, a_entity, a_id, b_entity, b_id, type, since, until, note, source)
```

Packs declare relation types with direction and labels:
- `spouse_of`, `decides_for`, `referred_by`
- `training_partner`, `parent_of` (for youth athletes)
- `room_with`, `travels_with`, `avoid_pairing`

Uses:
- Briefs show the people around a person ("Sara decides together with her husband Reza, and Reza dislikes old buildings").
- Referral counts become a metric.
- Room and table planning respects `avoid_pairing`.

### 3.4 New role `occasion` (an event with many participants)

`transaction` is one person paying for one thing. Viewings, sessions and tours are events that involve several people and things.

- Required: `date`. Optional: `time`, `end`, `place`, `item`, `staff`, `status`.
- Participants are stored in `occasion_participants(occasion, party, role, outcome, note)`, with outcomes such as attended, no_show, liked or rejected.
- Occasions can nest: a tour departure has days, and days have activities. A single `parent` ref handles this.
- Money can still be linked: an occasion can produce transactions, such as the tour price or the session fee.

### 3.5 New role `observation` (values over time)

- Required: `party`, `date`, `metric`, `value`. Optional: `unit`, `context`, `occasion`.
- The metric vocabulary comes from the pack: weight, squat_1rm, 5k_time, rpe, sleep_h, pain_0_10 for coaches. Users can extend it.
- `metrics.py` gains trend, personal best, plateau detection (no improvement in N weeks), rate of change and adherence (planned vs done sessions).
- Real estate uses observations too, for listing price history and offer history.

### 3.6 New role `item` and declarative match specs

`item` covers unique things that are matched to people: listings, programs, tour products, rooms. Matching is declared in the pack as validated JSON, in the same style as design patches, and is computed in code:

```json
{"match": "buyer_to_listing",
 "from": "buyer", "to": "listing",
 "rules": [
   {"from": "budget",  "to": "price",     "op": "within",   "hard": true, "tolerance": 0.05},
   {"from": "areas",   "to": "area",      "op": "in",       "hard": true},
   {"from": "rooms",   "to": "rooms",     "op": "within",   "weight": 3},
   {"fact": "must",    "to_tags": "features", "op": "covers",  "weight": 5},
   {"fact": "must_not","to_tags": "features", "op": "excludes","hard": true}
 ],
 "learn_from": "occasion_participants.outcome"}
```

- The result is a ranked list with reasons: "fits budget, 2 of 3 areas, has parking; missing: balcony".
- **Learning loop:** when the same buyer rejects 3 viewings for the same reason, the system proposes a new `must_not` fact. It never adds one silently.
- The same engine works in both directions (listing → buyers) and for other trades (participant → tour, athlete → program).

### 3.7 Watch rules (declarative alerts)

Rules are declared per pack, validated, and evaluated on the server whenever `get_context` runs. Their output becomes a line in context or a task, never a card.

| Rule shape | Example |
|---|---|
| `expires_before` | passport expires < 6 months after the tour end date |
| `repeated_fact` | "pain" mentioned ≥ 3 times in 14 days → suggest a referral |
| `stale_contact` | buyer in stage `negotiating`, no contact for 10 days |
| `threshold` | package sessions left ≤ 2 → renewal task |
| `conflict_in_occasion` | tomorrow's restaurant serves nuts, and a participant has a nut allergy |
| `trend` | adherence dropped 30% over 4 weeks |
| `date_soon` | birthday during the tour; anniversary of a purchase |

Users can add their own rules by chat ("warn me when any client hasn't trained for 10 days") through a new `change_schema` action `add_watch`.

### 3.8 Sensitivity, consent and the right to be forgotten

This matters because the product targets the EU, and GDPR Art. 9 treats health data as a special category.

- Fields and facts marked `special` are:
  - never shown in cards unless asked for by name;
  - left out of exports by default;
  - masked in `query_data` results.
- A `consent` fact kind records what each person agreed to and when. Packs can require consent before `special` facts are stored. The server stores the fact and adds a "consent missing" task instead of refusing.
- `forget_person` deletes the record, its facts, relations, observations, participations and aliases. It shows a preview first and keeps one undo window, after which the snapshot is purged.
- `export_person` produces one JSON/PDF file with everything held about a person, for subject access requests and as a service to the client.

---

## 4. The three packs

Each pack is one entry in `verticals.py`. It contains entities, fact kinds, relation types, observation metrics, match specs, watch rules, metrics, brief layout, demo data in `data/synth.py` with ground truth, and eval questions.

### 4.1 Real estate (`realestate`)

- **Entities**
  - `client` (party, with `side`: buyer / seller / renter / landlord, and a `stage` enum)
  - `listing` (item: price, area, m², rooms, floor, year, features tags, geo, status)
  - `viewing` (occasion with client participants and a per-person outcome)
  - `offer` (transaction-like: listing, client, amount, status)
  - `deal` (transaction: commission)
- **Fact kinds:** must, must_not, nice_to_have, motivation (why they are buying or selling), timeline, financing (pre-approved, cash, needs to sell first), decision_maker, communication_style, family.
- **Relations:** spouse_of, decides_for, referred_by, represents (lawyer).
- **Matching:** buyer ↔ listing in both directions, explained, and learning from viewing outcomes.
- **Watch rules:**
  - stale hot lead
  - new listing matches ≥ 1 active buyer
  - price drop on a listing a buyer rejected for price
  - seller listing with no viewing in 21 days
- **Metrics:** pipeline by stage, days in stage, viewings per deal, conversion by lead source, commission forecast, referral share.
- **Signature moments**
  - "New listing on Valea Ialomiței 12" → "Fits 3 buyers. Best: the Popescus (budget, area, 3 rooms; they asked for a balcony and this one has one)."
  - Before a viewing: "Ana decides, Mihai pays. Ana hated dark kitchens at the last 2 viewings. This one faces north."

### 4.2 Sports coach (`coach`)

- **Entities**
  - `athlete` (party: level, sport, `package` ref)
  - `session` (occasion: individual or group; planned vs done)
  - `measurement` (observation)
  - `program` (item: phase, focus, weeks)
  - `package` (custom: sessions bought / left, expiry)
  - `payment` (transaction)
- **Fact kinds:** goal (with target date), injury (body part, since, status), limitation, medication, motivation_style (data / praise / competition), schedule_constraint, nutrition, life_event (exams, new job, baby).
- **Relations:** parent_of (minors), training_partner, referred_by.
- **Observation metrics:** weight, body fat, girth, 1RM per lift, run times, RPE, sleep, mood, pain 0–10. All are extendable.
- **Watch rules:**
  - repeated pain
  - adherence drop
  - plateau
  - package ending or expired
  - no session in N days
  - goal date approaching while behind the trend
- **Metrics:** retention, adherence, progress towards goal, revenue per athlete, churn risk.
- **Signature moments**
  - Before a session: "Darius: knee pain 2 of the last 3 sessions (4/10 → 6/10). Squat PR was 3 weeks ago. Exams this week, so expect low energy."
  - Monthly progress report the coach can send to the athlete (PDF / page) with charts.

### 4.3 Tour leader (`tour`)

- **Entities**
  - `traveler` (party: passport no., nationality, passport expiry, DOB, emergency contact, insurance)
  - `tour` (item: product, itinerary)
  - `departure` (occasion: dates, capacity, with child occasions for days and activities)
  - `booking` (transaction: traveler, departure, paid, balance)
  - `room` (resource)
  - `supplier` (custom: hotel, restaurant, guide, with contact details)
- **Fact kinds:** diet, allergy (severity), medical, mobility, medication, interest, fear (heights, boats), sleep (snores, light sleeper), room_preference, past_complaint, celebration.
- **Relations:** travels_with, room_with, avoid_pairing, family_of.
- **Watch rules:**
  - documents expiring
  - unpaid balance close to departure
  - allergy or diet against the day's restaurant or menu
  - mobility against the activity difficulty
  - birthday during the trip
  - repeat traveler with a past complaint
- **Group tools**
  - group brief per day
  - room and table planner (respects relations and preferences; computed in code; shown as a card on request)
  - printable or offline manifest
- **Metrics:** occupancy per departure, repeat rate, satisfaction (if collected), revenue per departure, balance outstanding.
- **Signature moments**
  - Morning of day 3: "Today: Meteora (300 steps). Mr. Klein (knee) — offer the van to the upper monastery. Lunch at Taverna X: 2 vegetarian, 1 severe nut allergy (Ms. Ionescu, carries an EpiPen); restaurant notified? Birthday: Julie, 40."

---

## 5. Tools: extend, don't multiply

There are already 31 tools, so new behaviour goes into existing tools wherever possible.

| Change | Tool |
|---|---|
| New capture types `fact`, `relation`, `observation`, `occasion`, `participant_outcome` | `capture` |
| `get_customer_context` becomes `get_person_brief`: text first, with sensitive data masked, relations, open items, "what you don't know yet" | rename (keep an alias) |
| **New:** `brief_occasion(occasion or "next")`: per-person lines for everyone at the next viewing, session or tour day | 1 new tool |
| **New:** `find_matches(spec, record)`: ranked and explained | 1 new tool |
| `add_fact_kind`, `add_relation_type`, `add_watch`, `add_match` | new `change_schema` actions |
| `forget_person`, `export_person` | new `edit_records` actions |
| Watch rule hits, plus "today's people" in one line each | `get_context` |
| **New cards (on request only):** person card, match card, group board / room planner, progress chart, import progress | `views.py` / `view.html` |

---

## 6. Connectors

The server cannot call other connectors itself. Claude can, because the user has Gmail, Calendar, Drive, Notion and others attached. There are two ways to connect:

- **A. Claude as the bridge (now, no OAuth inside our server).** Project instructions and prompts tell Claude to read from a connector and pass the result to `capture` / `add_data`. Each item carries `source="gmail:<msgid>"`, so a re-run never duplicates anything.
- **B. Direct sync (later, for high-volume structured data).** The server pulls from an API with its own OAuth. This is only worth it for things like Strava or booking systems, where volume is high and the data is clean.

The new system table `sync_state(source, cursor, last_run, stats)` lets both ways sync **incrementally**: "since the last sync, 14 new emails from clients, 3 new facts, 1 contradiction".

### 6.1 Common to all three packs

| Connector | What it adds | Way |
|---|---|---|
| **Google Calendar** | Upcoming meetings → automatic `brief_occasion` on request; tasks → events; attendees → people matching | A |
| **Gmail** | Facts and tasks from client threads; draft follow-ups in the client's style | A |
| **Contacts (vCard / Google Contacts export)** | Seeds people, phones, birthdays | file |
| **WhatsApp / Telegram chat exports** | The richest source of personal facts for these trades | file (.txt / .json parser) |
| **Google Drive / Sheets, Notion** | Old client sheets, notes, intake forms | A and file |
| **Voice notes** (transcribed in Claude) | After-meeting capture in one take | A |
| **Forms (Google Forms / Typeform exports)** | Intake questionnaires → facts, with consent recorded | file |
| **Stripe / PayPal / Square** | Payments, packages, balances | A (plugins exist) |

### 6.2 By trade

| Trade | Connectors |
|---|---|
| Real estate | Portal listing exports and feeds (CSV/XML; RESO in the US; Idealista, Rightmove, Imobiliare.ro, ImmoScout24 exports); CRM exports (HubSpot, Pipedrive, Follow Up Boss); DocuSign status; maps / geocoding for `geo` and commute time to the buyer's work |
| Coach | Strava, Garmin Connect, Apple Health export, Google Fit, TrainingPeaks → `observation`; booking tools (Calendly, Mindbody, Glofox) → sessions; Trainerize / TrueCoach exports; MyFitnessPal for nutrition (opt-in, `special`) |
| Tour leader | Booking systems (FareHarbor, Rezdy, Bókun, TourRadar exports) → bookings and travelers; registration forms → passport, diet and medical facts (`special`); **Kiwi.com** (attached) for flight options; weather and maps for day briefs; WhatsApp group export after a tour → feedback and complaints |

Each connector gets a **mapping profile**. `mapping_memory` already remembers column layouts; it is extended with source-specific presets, so a Follow Up Boss export or a FareHarbor manifest maps in one step without questions.

---

## 7. Getting started: bringing in existing data

The biggest risk for these users is **the cold start**. Their knowledge sits in WhatsApp, their head, a notebook and a messy spreadsheet. Two paths, chosen in `get_started`, can be mixed.

### 7.1 "Everything now" (big-bang import)

1. **Drop everything.** The user gives a folder or zip with contacts, spreadsheets, chat exports, PDFs and old Claude/ChatGPT chats.
2. **Inventory card.** Example: "412 contacts, 38 WhatsApp chats, 2 sheets (buyers, listings), 1 Claude export. About 20 minutes. People first, then records, then facts from chats." The user can untick sources.
3. **People first.** Identity resolution across all sources (the existing alias engine, plus phone and email from contacts) builds the person list before anything else, so facts attach to the right person.
4. **Structured data next.** Sheets and exports go through `add_data` with mapping profiles.
5. **Facts from conversations last.** These are extracted in batches through the existing `history_queue`, each with quote, date and `confidence=stated`.
6. **Review in bulk, not one by one.** Facts and contradictions are grouped per person, and one card confirms or dismisses a whole group. Low-impact facts stay `stated` and appear in briefs with a "?" until confirmed.
7. **Finish screen.** It shows a completeness score per person ("knows 9 of 12 key things about the Popescus; missing: financing, timeline, decision maker") and the 10 most important gaps to fill.

### 7.2 "Little by little" (drip import, recommended default)

Most users won't spend an evening importing. The system builds knowledge **just in time**:

- **Start with this week.** It reads the calendar (or asks "who will you see this week?") and builds those 5–15 people first.
- **Before each meeting.** "I know little about Ana. Send me your WhatsApp chat with her (Export chat → without media) or tell me 3 things." It takes 1 minute and gives the biggest return.
- **After each meeting.** One voice note is turned into facts, outcome and tasks.
- **Daily nudge in context** (quiet, one line): "3 active clients have empty profiles: Mihai, Elena, Paul."
- **Guided interview.** A 5-minute question set per person, chosen from the pack's key fact kinds that are still missing. It skips anything already known.
- **Background backfill.** When the user has a moment: "import the next 10 chats".

### 7.3 Shared building blocks

- **Demo first.** Each pack ships a realistic demo dataset (3 agents, 1 coach, 1 tour company with ground truth). The user can play for 2 minutes, then "clear demo and use my data".
- **Completeness model.** Packs declare the key facts per role (buyer: budget, areas, timeline, financing, decision maker…). This drives the score, the interview questions and the "what you don't know" line in briefs.
- **Idempotent imports.** Every row and fact carries its source id, so importing the same chat twice changes nothing, and an updated sheet only adds the difference.
- **Undo the whole import** in one step (a snapshot per import batch).

---

## 8. Making it attractive

All of these appear only when the user asks to see them:

- **Person card**
  - one headline line: "the one thing to remember"
  - colored chips for must / must-not / sensitive
  - a timeline of contacts and outcomes
  - the people around them
  - "what I don't know yet"
- **Match card:** ranked listings or buyers with ✓ / ✗ reasons and a "send to client" draft.
- **Group board:** a tour or class at a glance (diet, allergy and mobility icons), with a drag-free room / table planner proposed by the server.
- **Progress card:** athlete trends, PRs and goal line. It can be exported as a client-facing report.
- **Morning brief** (text, or a card on request): "Today you'll see 4 people", one line each, plus watch-rule hits.
- **Quiz me:** before a big meeting, Claude asks 3 questions about the person.
- **Relationship warmth:** a simple signal (contact recency × stage) on the person card, and a "going cold" list on request.
- **Small celebrations:** first deal, an athlete's PR, the 10th repeat traveler. One line in context, so the user feels the system is on their side.

---

## 9. Phases

Each phase ships with tests, demo data, eval questions and translations in all 4 languages.

| Phase | Scope | Done when |
|---|---|---|
| **P1 — People layer** | `facts`, `relations`, fact kinds, contradiction questions, sensitivity masking, `get_person_brief`, capture types `fact` / `relation`, `forget_person` / `export_person` | Existing 3 packs still pass; new tests for supersede, masking and forget; eval: "what does X dislike?" answered with source |
| **P2 — Engine roles and types** | `range`, `tags`, `duration`, `geo`, `file`; roles `occasion`, `observation`, `item`; role views; metric functions (trend, PR, plateau, adherence) | Schema undo works for every new type; convert text → tags/range works |
| **P3 — Matching and watch rules** | Match spec validator and engine with explanations and learning proposals; watch rule engine plus `add_watch`; hits in `get_context`; `brief_occasion` | Ground-truth tests: match ranking and rule hits on demo data are exact |
| **P4 — Real estate pack** | Pack, demo agency, portal and CRM mapping presets, match card, person card | Eval set of 30 agent questions passes |
| **P5 — Onboarding import** | Inventory card, WhatsApp/Telegram parser, vCard, people-first pipeline, grouped review, completeness model, drip nudges, interview mode, `sync_state` | A messy demo folder imports end to end; re-import is a no-op; undo of the whole import is exact |
| **P6 — Coach pack** | Pack, observations, progress card, client report, Strava / Health export importers | Plateau / pain / adherence rules exact on demo data |
| **P7 — Tour pack** | Pack, nested occasions, group board, room planner, manifest export, booking-system presets | Daily brief catches all planted conflicts in the demo tour |
| **P8 — Connector bridges** | Prompts and Project instructions for Gmail / Calendar / Drive pulls, source ids, incremental sync summaries; optional direct Strava sync | A second sync adds only new items |

The order is deliberate. P1–P3 are generic and unlock all three packs. Real estate comes first because matching is immediate, visible value. Import (P5) comes before the other packs because it decides whether anyone gets past day one.

---

## 10. Open questions

1. **Languages.** Packs ship in en / fr / ro / de. Should we add Persian (or another market) for these trades? RTL affects `view.html`.
2. **Multi-user.** Agencies and tour companies have teams. Do we stay single-owner for now, or plan shared memory? That affects storage and permissions.
3. **Direct API sync.** Is Strava / booking-system OAuth worth the maintenance in P8, or are file exports enough for the first version?
4. **Client-facing outputs.** Progress reports and listing shortlists sent to clients raise branding and privacy questions. What should the default be?
