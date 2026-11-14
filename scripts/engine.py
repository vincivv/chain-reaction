"""Decision engine: for one world event, decide for every supplier site in reach whether production is disrupted.
Drex makes three typed decisions per site (disrupted yes/no, severity scale, action), many sites per call."""
import json, math, os, sys, time, concurrent.futures as cf
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts")); import drex
import glob
DATA = json.load(open(os.path.join(ROOT, "data", "sites.json")))
SITES = DATA["sites"]; DOCUMENT = DATA["document"]
for _s in SITES: _s.setdefault("address", None)
_companies = {"t": 0, "v": None}
def companies():
    """Apple from sites.json plus any data/sites_<key>.json; re-read when files change."""
    files = sorted(glob.glob(os.path.join(ROOT, "data", "sites_*.json")))
    stamp = tuple(os.path.getmtime(f) for f in files)
    if _companies["v"] is not None and _companies["t"] == stamp: return _companies["v"]
    out = {"apple": {"key": "apple", "name": "Apple", "fictional": False, "document": DOCUMENT, "sites": SITES}}
    for f in files:
        try:
            d = json.load(open(f))
            for x in d["sites"]:
                x.setdefault("address", None); x.setdefault("precision", "city"); x.setdefault("page", None); x.setdefault("makes", ""); x.setdefault("category", "other")
            out[d["key"]] = {"key": d["key"], "name": d["name"], "fictional": bool(d.get("fictional")), "document": d.get("document") or {}, "sites": d["sites"]}
        except Exception as e: print("could not load", f, e)
    _companies.update(t=stamp, v=out)
    return out
THRESH = {"alert": 0.6, "review": 0.3}
LEGEND = ["no impact", "inspection pause (hours)", "partial shutdown (days)", "extended shutdown (weeks)"]
ACTIONS = {"monitor": "No action needed beyond watching for updates", "contact_supplier": "Contact the supplier to confirm status",
           "activate_backup": "Start moving orders to a backup supplier"}
MODEL = os.environ.get("DREX_MODEL", "drex-max")
PRICE_PER_M_INPUT = float(os.environ.get("DREX_PRICE_PER_M", "0.12"))     # USD per million input tokens; output is not billed
CHUNK = 12
FORMULA = ["Shaking at each site (Modified Mercalli, 1-10), empirical attenuation after Bakun & Wentworth (1997):",
           "MMI = 1.68·M − 3.29 − 0.0206·R", "R = √(d² + h²)   d = distance to epicentre (km), h = depth (km)",
           "Approximate: sites are placed at their region's main city, not at factory addresses."]

def haversine(a, b, c, d):
    p = math.pi / 180; x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))
def mmi(mag, dist_km, depth_km):
    r = math.sqrt(dist_km ** 2 + (depth_km or 10) ** 2)
    return max(1.0, min(10.0, 1.68 * mag - 3.29 - 0.0206 * r))
def reach_km(ev):
    if ev["kind"] == "earthquake" and ev.get("magnitude"):
        return max(80.0, min(700.0, (1.68 * ev["magnitude"] - 3.29 - 3.5) / 0.0206))      # out to where shaking falls to about MMI 3.5
    return {"cyclone": 350.0, "flood": 250.0, "volcano": 150.0, "wildfire": 120.0,
            "strike": 150.0, "conflict": 300.0, "port": 250.0, "policy": 600.0}.get(ev["kind"], 300.0)

def build_state(ev, cands):
    e = {k: ev.get(k) for k in ("kind", "title", "summary", "place", "magnitude", "depth_km", "alert") if ev.get(k) is not None}
    e["when"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ev["time"] / 1000))
    return {"event": e, "sites": [{"ref": f"S{i+1}", "supplier": c["supplier"], "makes": c["makes"], "category": c["category"],
            "region": c["region"], "country": c["country"], "distance_km": round(c["distance_km"]),
            "location_precision": c.get("precision", "region"),
            **({"shaking_intensity_mmi": round(c["intensity"], 1)} if c["intensity"] is not None else {})} for i, c in enumerate(cands)]}
def questions(n):
    q = {}
    for i in range(n):
        r = f"S{i+1}"
        q[f"d_{r}"] = {"type": "noul", "instructions": f"Will site {r} stop or slow production for at least one day because of this event?"}
        q[f"s_{r}"] = {"type": "score", "instructions": f"How badly is production at site {r} affected by this event?", "criteria": LEGEND}
        q[f"a_{r}"] = {"type": "choice", "instructions": f"What should the buyer's supply team do about site {r} now?", "criteria": ACTIONS}
    return q

def candidates(ev, sites=None):
    reach = reach_km(ev); out = []
    for s in (SITES if sites is None else sites):
        d = haversine(ev["lat"], ev["lon"], s["lat"], s["lon"])
        if d <= reach:
            out.append({**s, "distance_km": d, "intensity": mmi(ev["magnitude"], d, ev.get("depth_km")) if ev["kind"] == "earthquake" and ev.get("magnitude") else None})
    out.sort(key=lambda c: c["distance_km"])
    return out, reach

def assess(ev, sites=None):
    cands, reach = candidates(ev, sites)
    chunks = [cands[i:i + CHUNK] for i in range(0, len(cands), CHUNK)]
    t = time.time()
    def run(ch):
        return drex.systemone(build_state(ev, ch), questions(len(ch)), model=MODEL)
    res = []
    if chunks:
        with cf.ThreadPoolExecutor(min(6, len(chunks))) as ex: res = list(ex.map(run, chunks))
    wall = round((time.time() - t) * 1000)
    impacts = []
    for ch, r in zip(chunks, res):
        a = r["answers"]
        for i, c in enumerate(ch):
            ref = f"S{i+1}"; d, s, act = a[f"d_{ref}"], a[f"s_{ref}"], a[f"a_{ref}"]
            probs = s.get("probabilities") or {}
            sp = [float(probs.get(str(k), probs.get(LEGEND[k], 0))) for k in range(4)]
            p = float(d["noul"])
            impacts.append({"site_id": c["id"], "supplier": c["supplier"], "country": c["country"], "region": c["region"], "lat": c["lat"], "lon": c["lon"],
                            "page": c["page"], "makes": c["makes"], "category": c["category"], "address": c.get("address"), "precision": c.get("precision", "region"),
                            "distance_km": round(c["distance_km"], 1),
                            "intensity": round(c["intensity"], 1) if c["intensity"] is not None else None, "p_disrupted": p,
                            "severity": {"score": float(s.get("score", 0)), "label": LEGEND[max(range(4), key=lambda k: sp[k])], "legend": LEGEND, "probabilities": sp},
                            "action": {"choice": act["choice"], "probabilities": act["probabilities"]},
                            "tier": "alert" if p >= THRESH["alert"] else "review" if p >= THRESH["review"] else "clear"})
    impacts.sort(key=lambda x: -x["p_disrupted"])
    tokens = sum(r["usage"]["input_tokens"] for r in res)
    exp = {t_: sum(1 for x in impacts if x["tier"] == t_) for t_ in ("alert", "review", "clear")}
    exp["suppliers_alert"] = len({x["supplier"] for x in impacts if x["tier"] == "alert"})
    return {"event": ev, "candidates": len(cands), "reach_km": round(reach),
            "drex": {"model": res[0].get("model") if res else MODEL, "calls": len(res), "decisions": 3 * len(cands), "ms": wall,
                     "eval_ms": round(sum(r.get("evaluation_time_ms", 0) for r in res)), "input_tokens": tokens,
                     "cost_usd": round(tokens * PRICE_PER_M_INPUT / 1e6, 6)},
            "impacts": impacts, "exposure": exp, "formula": FORMULA if ev["kind"] == "earthquake" else []}

if __name__ == "__main__":
    scen = json.load(open(os.path.join(ROOT, "data", "backtest.json")))
    for ev in scen:
        r = assess(ev); print(f"\n{ev['id']}  M{ev['magnitude']}  candidates {r['candidates']} reach {r['reach_km']} km  drex {r['drex']}")
        print("  exposure", r["exposure"])
        for x in r["impacts"][:6]: print(f"   {x['p_disrupted']:.2f} {x['tier']:6s} {x['supplier'][:38]:38s} {x['region'][:12]:12s} {x['distance_km']:6.0f} km MMI {x['intensity']} | {x['severity']['label']} | {x['action']['choice']} | {x['category']}")
        want = [c for c in ev["reported"] if c.get("in_supplier_list")]
        for c in want:
            names = [c["company"]] + c.get("aliases", [])
            hit = [x for x in r["impacts"] if any(n.lower() in x["supplier"].lower() or x["supplier"].lower() in n.lower() for n in names)]
            best = max(hit, key=lambda x: x["p_disrupted"]) if hit else None
            print(f"   reported: {c['company']:14s} -> " + (f"{best['tier']} p={best['p_disrupted']:.2f} ({best['region']}, {best['distance_km']:.0f} km)" if best else "NOT IN REACH"))
