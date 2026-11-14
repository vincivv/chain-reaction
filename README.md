# Chain Reaction

A live map that answers one question: which of your suppliers did the world just hit?

The app reads a company's supplier list, watches earthquakes and disaster alerts as they come in, and decides for every supplier site in reach whether production is likely to be disrupted, how badly, and what the supply team should do next. Each site lands in one of three tiers: alert, human review, or clear.

Built at Drexathon (Nace AI), 14 Nov 2026. Track: Apex Predator.

## How it uses each product

| Product | What it does here |
|---|---|
| **NDI** | Parsed Apple's public Supplier List for fiscal year 2023 (a 27-page PDF) in about 5 seconds into 187 suppliers and 760 supplier locations, each with its page number. |
| **Drex Max** | Makes three typed decisions per site for each event: will production stop or slow for at least a day (yes/no probability), severity on a 4-level scale, and the next action from 3 options. 12 sites per call, calls in parallel. |
| **Intern + DeepInfra** | Intern writes the incident brief, with Qwen 3.8 27B on DeepInfra as the worker and Drex as the supervisor. If Intern is not installed, the server calls DeepInfra directly. |
| **DeepInfra (Qwen 3.8 27B)** | Added a one-line "what this supplier makes" for all 187 suppliers, and region coordinates. |
| **Claude (comparison)** | `scripts/claude_race.py` runs the same decisions through Claude Opus 5.5 for time, cost and agreement. Measured on 14 Nov 2026 across the four past incidents (12 sites each, 48 sites): Drex Max took 1.5 s and $0.0014, Claude Opus 5.5 took 17.8 s and $0.10, so Drex was about 11 times faster and about 75 times cheaper. The two agreed on the disrupted yes/no call for 79% of sites (42% on the Taiwan quake, where Drex flags more sites than Claude; 83% to 100% on the other three). Numbers are in `data/race.json`. |

## How a decision is made

- **Events:** live USGS earthquakes (M4.5+, past week) and GDACS orange and red alerts (cyclones, floods, volcanoes, wildfires), refreshed every minute. Only events with supplier sites in reach are shown.
- **Reach:** for an earthquake, out to where estimated shaking falls to about intensity 3.5; fixed radii for other event types.
- **Shaking at each site:** estimated with an empirical attenuation formula (after Bakun & Wentworth 1997) from magnitude, distance and depth.
- **What Drex sees:** the event, and for each site the supplier, what it makes, its region, distance, and estimated shaking.
- **Tiers:** alert at 60% and above, human review from 30% to 60%, clear below.

Supplier locations are region-level (province, prefecture, state). Each site is placed at its region's main city, not at a factory address.

## Measured results

| What | Result |
|---|---|
| Taiwan 2024 earthquake: 48 sites, 144 decisions | about 0.5 to 0.7 seconds, about $0.0014 |
| All four past incidents: 399 decisions | about 1.9 seconds |
| Drex Max price used | $0.12 per million input tokens (output is not billed) |

Times vary a little from run to run. Exact figures from the last run are in `data/benchmark.json`.

## Test on four real past earthquakes

Taiwan (Apr 2024, M7.4), Noto (Jan 2024, M7.5), Fukushima (Mar 2022, M7.3) and Kumamoto (Apr 2016, M7.0). Ground truth is in `data/backtest.json`: companies that publicly reported a production impact.

Of the companies that reported an impact **and** that Apple's list places in the affected area, the app put 5 of 5 on alert: TSMC (Taiwan), Murata (Noto), Murata and Sony (Fukushima), Sony (Kumamoto).

Limits of this test:

- **Tiny sample.** Five cases is not a benchmark.
- **Four other reported companies are on the list but with no site listed in that area** (Japan Display, Renesas twice, Kioxia), so the app could not flag them.
- **Six reported companies are not on Apple's list at all.**
- **It over-flags.** Because locations are region-level, every supplier in a hit region looks the same distance away. For Taiwan about 30 suppliers went on alert.
- **Ground truth came from search summaries**, not from reading the articles.
- **The Kumamoto test uses a 2023 supplier list against a 2016 event.**

## Run it

Needs Python 3 with `flask`.

1. Create `.env` with `NACE_API_KEY` and `DEEPINFRA_API_KEY`. `ANTHROPIC_API_KEY` is optional and only enables the Claude comparison.
2. Run the four past incidents from the command line:
   ```bash
   python3 scripts/engine.py
   ```
3. Start the app, then open http://localhost:5188:
   ```bash
   python3 app/server.py
   ```

`data/sites.json` is included. To rebuild it from the PDF, run `python3 scripts/build_sites.py` and then `python3 scripts/fix_geo.py`.

The name and tagline are in `app/config.json`.

## Credits

- Apple Supplier List, fiscal year 2023 (public PDF)
- USGS earthquake feeds
- GDACS disaster alerts

## Added on the day (after the first write-up)

- **Second company with exact addresses:** adidas's public Global Supplier List (1 July 2026 spreadsheet). NDI parsed it in about 6 seconds; 500 of its 836 rows are loaded (every Middle East row, all primary suppliers, a spread of the rest), each with its street address and placed at city level. 24 sites are in Turkey, Egypt and Jordan.
- **News events:** labour strikes, military strikes and unrest, port disruptions and export bans, from GDELT headlines of the past two days. Qwen 3.8 on DeepInfra classifies each headline and places it on the map; Drex then makes the same three decisions per supplier site. Coverage is thin (a handful of located events at a time) and headline-level only.
- **Always-on watch:** the server assesses every live event against every company in the background and the map shows all of them at once, with a company switcher.
