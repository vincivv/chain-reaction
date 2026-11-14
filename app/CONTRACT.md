# Server / UI contract

Server: `python3 app/server.py` serves http://localhost:5188. Static files from `app/static/` (`index.html` at `/`, `world.json` TopoJSON at `/static/world.json`, object `objects.countries`).
The product name comes from `GET /api/state` (`name`); do not hard-code it anywhere in the UI.

## GET /api/state
```jsonc
{
  "name": "Faultline",
  "tagline": "Which of your suppliers did the world just hit?",
  "company": "Apple",
  "document": { "title": "Apple Supplier List, fiscal year 2023", "url": "https://…pdf", "pages": 27 },
  "sites": [{ "id": 17, "supplier": "Taiwan Semiconductor Manufacturing Company", "country": "Taiwan", "region": "Hsinchu",
              "lat": 24.8, "lon": 120.97, "page": 23 }],          // every supplier location read from the document by NDI
  "counts": { "suppliers": 187, "sites": 640 },
  "thresholds": { "alert": 0.6, "review": 0.3 },                  // on p_disrupted
  "claude_enabled": false                                        // true when a Claude key is configured for the comparison
}
```

## GET /api/events
```jsonc
{
  "live": [{ "id": "usgs:us7000abcd", "kind": "earthquake", "title": "M 6.1 - 20 km E of Hualien City, Taiwan",
             "time": 1794690000000, "lat": 23.9, "lon": 121.8, "magnitude": 6.1, "depth_km": 12, "alert": "orange",
             "url": "https://…", "source": "USGS" }],               // newest first; kinds: earthquake | cyclone | flood | volcano | wildfire | news
  "scenarios": [{ "id": "scn:taiwan-2024", "kind": "earthquake", "title": "M 7.4 - 15 km S of Hualien City, Taiwan",
                  "time": 1712102292173, "lat": 23.84, "lon": 121.6, "magnitude": 7.4, "depth_km": 40,
                  "url": "https://…", "source": "USGS", "note": "3 Apr 2024. Real past incident used as a test." }]
}
```
`news` events may have `lat`/`lon` null.

## POST /api/assess   body `{ "event_id": "…" }`
Runs Drex over every supplier site the event could reach. Usually 1 to 4 seconds.
```jsonc
{
  "event": { /* same shape as above */ },
  "candidates": 96,                       // sites inside the reach radius, each one gets Drex decisions
  "drex": { "model": "drex-max-v1.1", "calls": 4, "decisions": 288, "ms": 1320, "input_tokens": 41000, "cost_usd": 0.0041 },
  "impacts": [{                           // sorted by p_disrupted, highest first; includes every candidate
    "site_id": 17, "supplier": "…", "country": "Taiwan", "region": "Hsinchu", "lat": 24.8, "lon": 120.97, "page": 23,
    "distance_km": 112.4,
    "intensity": 6.3,                     // estimated shaking at the site (Modified Mercalli, 1-10); null for non-earthquakes
    "p_disrupted": 0.81,                  // Drex: production stops or slows for at least a day
    "severity": { "score": 1.9, "label": "partial shutdown (days)",
                  "legend": ["no impact", "inspection pause (hours)", "partial shutdown (days)", "extended shutdown (weeks)"],
                  "probabilities": [0.05, 0.22, 0.51, 0.22] },
    "action": { "choice": "contact_supplier", "probabilities": { "monitor": 0.1, "contact_supplier": 0.7, "activate_backup": 0.2 } },
    "tier": "alert"                       // "alert" | "review" | "clear"
  }],
  "exposure": { "alert": 12, "review": 31, "clear": 53, "suppliers_alert": 9 },
  "formula": ["MMI = 3.66·M − 1.66·log10(R) − 8.4 …", "R = sqrt(d² + h²)"],   // lines to show in a small "how intensity is estimated" panel; may be empty
  "reach_km": 400
}
```

## POST /api/brief   body `{ "event_id": "…" }`
Incident brief for the last assessment of that event, written by the Intern agent (DeepInfra worker, Drex supervising), or DeepInfra directly if Intern is not installed. 3 to 30 seconds.
```jsonc
{ "text": "…", "engine": "intern" | "deepinfra", "secs": 6.2 }
```

## POST /api/race   body `{ "event_id": "…" }`
Same decisions on a sample of sites through Drex and through Claude. 5 to 40 seconds. `claude` is null when no key is set.
```jsonc
{ "sample": 12,
  "drex":   { "model": "drex-max-v1.1", "ms": 410, "cost_usd": 0.0004, "decisions": 36 },
  "claude": { "model": "claude-opus-5-5", "ms": 21800, "cost_usd": 0.31, "decisions": 36, "agreement": 0.83 },
  "speedup": 53.2, "cheaper": 775.0 }
```

## GET /api/benchmark
```jsonc
{ "scenarios": [{ "id": "scn:taiwan-2024", "title": "…", "reported": ["TSMC", "…"], "flagged": ["…"], "hits": 5, "total": 6,
                  "note": "Reported = suppliers that publicly said production was affected." }],
  "totals": { "decisions": 1200, "ms": 5200, "cost_usd": 0.012 } }
```

Errors: any endpoint may return `{ "error": "message" }` with a 4xx/5xx status.
