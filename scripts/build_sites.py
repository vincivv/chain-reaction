"""Turn NDI's parse of the supplier list PDF into sites.json: one row per supplier location, with page number,
coordinates (region-level, from Open-Meteo's free geocoder) and what the supplier makes (one line, from an LLM on DeepInfra)."""
import json, os, re, sys, time, urllib.request, urllib.parse, concurrent.futures as cf
from html.parser import HTMLParser
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts")); import drex
D = lambda *p: os.path.join(ROOT, "data", *p)
doc = json.load(open(D("cache", "supplier_parse.json")))["result"]["document"]

class Rows(HTMLParser):
    def __init__(s): super().__init__(); s.rows = []; s.cur = None; s.cell = None
    def handle_starttag(s, t, a):
        if t == "tr": s.cur = []
        elif t in ("td", "th"): s.cell = ""
    def handle_endtag(s, t):
        if t in ("td", "th") and s.cur is not None: s.cur.append(re.sub(r"\s+", " ", s.cell or "").strip()); s.cell = None
        elif t == "tr" and s.cur is not None: s.rows.append(s.cur); s.cur = None
    def handle_data(s, d):
        if s.cell is not None: s.cell += d

rows = []     # (supplier, regions, country, page)
supplier = None
for part in re.split(r"--- Page (\d+) ---", doc["markdown"])[1:][1::2] and zip(*[iter(re.split(r"--- Page (\d+) ---", doc["markdown"])[1:])] * 2):
    page, text = int(part[0]), part[1]
    p = Rows(); p.feed(text)
    for cells in p.rows:
        if any("SUPPLIER NAME" in c.upper() for c in cells): continue
        cells = [c for c in cells]
        if len(cells) >= 3:
            supplier = cells[-3].lstrip("*").strip() or supplier
            loc, country = cells[-2], cells[-1]
        elif len(cells) == 2: loc, country = cells
        else: continue
        if not supplier or not country: continue
        rows.append((supplier, [r.strip() for r in loc.split(",") if r.strip()], country, page))
print("rows", len(rows), "suppliers", len({r[0] for r in rows}))

# ---- geocode unique (region, country)
def geocode(key):
    region, country = key
    cq = {"China mainland": "China"}.get(country, country)
    for name in ([region] if region != country else []) + [cq]:
        try:
            u = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": name, "count": 10, "language": "en"})
            res = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "hackathon-demo/0.1"}), timeout=20)).get("results") or []
            same = [r for r in res if (r.get("country") or "").lower() in (cq.lower(), country.lower()) or cq.lower() in (r.get("country") or "").lower()]
            pick = (same or ([] if name == region else res))
            if pick:
                # prefer administrative areas over small towns of the same name
                pick.sort(key=lambda r: (0 if str(r.get("feature_code", "")).startswith(("ADM", "PCL")) else 1, -(r.get("population") or 0)))
                return key, (pick[0]["latitude"], pick[0]["longitude"], "region" if name == region else "country")
        except Exception: pass
    return key, None
keys = sorted({(reg, c) for s, regs, c, pg in rows for reg in regs})
with cf.ThreadPoolExecutor(8) as ex: geo = dict(ex.map(geocode, keys))
miss = [k for k, v in geo.items() if not v]
print("unique locations", len(keys), "geocoded", len(keys) - len(miss), "missing", miss[:12])

# ---- what each supplier makes (one short line), from Qwen on DeepInfra
def llm(prompt):
    req = urllib.request.Request("https://api.deepinfra.com/v1/openai/chat/completions", data=json.dumps({
        "model": "Qwen/Qwen3.8-27B", "max_tokens": 3000, "temperature": 0, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": "Bearer " + drex.ENV["DEEPINFRA_API_KEY"], "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]
names = sorted({r[0] for r in rows}); makes = {}
def enrich(chunk):
    out = llm("For each company below (suppliers to a consumer-electronics maker), give what it mainly makes in at most 6 words and one category from: "
              "semiconductor fab, chip packaging, display, assembly, PCB, passive components, battery, optics/camera, materials/chemicals, metal/enclosures, connectors/cables, other. "
              "Reply with only a JSON object mapping the exact company name to {\"makes\": \"...\", \"category\": \"...\"}. If unsure, use category \"other\".\n\n" + "\n".join(chunk))
    m = re.search(r"\{.*\}", out, re.S)
    try: return json.loads(m.group(0)) if m else {}
    except Exception: return {}
chunks = [names[i:i + 40] for i in range(0, len(names), 40)]
with cf.ThreadPoolExecutor(5) as ex:
    for d in ex.map(enrich, chunks): makes.update(d)
print("enriched", len(makes), "of", len(names))

sites = []
for s, regs, c, pg in rows:
    for reg in regs:
        g = geo.get((reg, c))
        if not g: continue
        m = makes.get(s) or {}
        sites.append({"id": len(sites), "supplier": s, "country": c, "region": reg, "lat": round(g[0], 3), "lon": round(g[1], 3),
                      "precision": g[2], "page": pg, "makes": m.get("makes", ""), "category": m.get("category", "other")})
json.dump({"document": {"title": "Apple Supplier List, fiscal year 2023", "pages": doc["page_count"],
                        "url": "https://s203.q4cdn.com/367071867/files/doc_downloads/2024/04/Apple-Supplier-List.pdf"},
           "sites": sites}, open(D("sites.json"), "w"), indent=0)
print("sites", len(sites), "suppliers", len({x["supplier"] for x in sites}))
for x in sites[:3] + [x for x in sites if "Taiwan Semi" in x["supplier"]][:3]: print(x)
