# Contract v2 (adds to CONTRACT.md; where they differ, this file wins)

## Company parameter
Every endpoint takes a company key: `?company=KEY` on GETs, `"company": "KEY"` in POST bodies. Omitted = the first company.

## GET /api/state?company=KEY
As before, plus:
```jsonc
{
  "company_key": "apple",
  "companies": [{ "key": "apple", "name": "Apple", "fictional": false,
                  "document": { "title": "…", "url": "…", "pages": 27 }, "counts": { "suppliers": 187, "sites": 760 } }],
  "sites": [{ "id": 17, "supplier": "…", "country": "…", "region": "…", "lat": 0, "lon": 0, "page": 23,
              "address": "street address or null", "precision": "address" | "city" | "region" }]   // only this company's sites
}
```
Site ids are unique across companies.

## GET /api/events?company=KEY
Same shape. New event kinds besides earthquake / cyclone / flood / volcano / wildfire:
`strike` (labour strike), `conflict` (military strike, attack, unrest), `port` (port or shipping disruption), `policy` (export ban, sanctions, tariffs).
News events also carry `"summary": "one line"` and `"source": "GDELT · reuters.com"`. Every live event has `sites_in_reach` and `note`.

## GET /api/overview?company=KEY      (new: the always-on global picture)
The server assesses every live event in the background and refreshes about once a minute. Poll this every 30 s.
```jsonc
{
  "updated": 1794690000000,
  "warming": false,                          // true while the first pass is still running; events may be empty
  "events": [{                               // only events with at least one supplier site in reach; most exposed first
    "event": { /* event object */ },
    "reach_km": 350,
    "exposure": { "alert": 1, "review": 0, "clear": 7, "suppliers_alert": 1 },
    "headline": "1 supplier on alert",
    "impacts": [ /* same objects as /api/assess impacts, tiers alert and review only, at most 40 */ ]
  }],
  "totals": { "events": 6, "alert": 14, "review": 9, "suppliers_alert": 11, "decisions": 420, "ms": 2100, "cost_usd": 0.004 }
}
```

## POST /api/assess, /api/brief, /api/race
Bodies gain `"company"`. `/api/race` is cached per company and event: call it automatically after every assess and show the bars; send `"force": true` from the button to re-run.
