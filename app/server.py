"""Server for the supply-chain risk map. Name and tagline live in app/config.json so the product can be renamed in one place."""
import json, math, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from flask import Flask, jsonify, request, send_from_directory
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import drex, engine
try: import claude_race
except Exception: claude_race = None
try: import news
except Exception: news = None
CFG = json.load(open(os.path.join(ROOT, "app", "config.json")))
D = lambda *p: os.path.join(ROOT, "data", *p)
SCENARIOS = json.load(open(D("backtest.json")))
app = Flask(__name__, static_folder=os.path.join(ROOT, "app", "static"), static_url_path="/static")
LAST = {}                 # (company, event_id) -> last assessment
EVENTS = {}               # event_id -> event
FEED = {"t": 0, "items": [], "news_t": 0, "news": []}
OVERVIEW = {}             # company -> overview payload
RACES = {}                # (company, event_id) -> race result
UA = {"User-Agent": "hackathon-demo/0.1"}
NEWS_KINDS = ("strike", "conflict", "port", "policy")

def _get(url, timeout=20):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))

def co():
    """Company key for this request."""
    k = request.args.get("company") or (request.get_json(silent=True) or {}).get("company")
    cs = engine.companies()
    return k if k in cs else next(iter(cs))

def _in_reach(ev, sites):
    if ev.get("lat") is None: return 0
    r = engine.reach_km(ev)
    return sum(1 for s in sites if engine.haversine(ev["lat"], ev["lon"], s["lat"], s["lon"]) <= r)

def refresh_feeds(force=False):
    """USGS earthquakes + GDACS alerts every minute; news every 10 minutes (the news source is rate-limited)."""
    if not force and time.time() - FEED["t"] < 60 and FEED["items"]: return FEED["items"]
    items = []; usgs, gdacs = [], []
    try:
        for f in _get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson")["features"]:
            p, g = f["properties"], f["geometry"]["coordinates"]
            usgs.append({"id": "usgs:" + f["id"], "kind": "earthquake", "title": p["title"], "time": p["time"], "lat": g[1], "lon": g[0],
                          "magnitude": p["mag"], "depth_km": g[2], "alert": p.get("alert"), "url": p["url"], "source": "USGS"})
    except Exception as e: print("usgs failed:", e)
    try:
        frm = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400)); to = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
        kinds = {"TC": "cyclone", "FL": "flood", "VO": "volcano", "WF": "wildfire"}
        for f in _get(f"https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC;FL;VO;WF&alertlevel=orange;red&fromDate={frm}&toDate={to}").get("features", []):
            p, g = f["properties"], f["geometry"]["coordinates"]
            if p.get("eventtype") not in kinds: continue
            t = int(time.mktime(time.strptime(p["fromdate"][:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
            gdacs.append({"id": f"gdacs:{p.get('eventtype')}{p.get('eventid')}", "kind": kinds[p["eventtype"]], "title": p.get("name") or p.get("description"),
                          "time": t, "lat": g[1], "lon": g[0], "magnitude": None, "depth_km": None, "alert": (p.get("alertlevel") or "").lower(),
                          "url": (p.get("url") or {}).get("report") or "https://www.gdacs.org", "source": "GDACS"})
    except Exception as e: print("gdacs failed:", e)
    if usgs: FEED["usgs"] = usgs
    if gdacs: FEED["gdacs"] = gdacs
    if usgs or gdacs:         # snapshot to disk so a restart during a network hiccup still has the last known events
        try: json.dump({"usgs": FEED.get("usgs", []), "gdacs": FEED.get("gdacs", [])}, open(D("cache", "feeds.json"), "w"))
        except Exception: pass
    items = FEED.get("usgs", []) + FEED.get("gdacs", []) + FEED["news"]          # last good result per source; news is filled by news_loop in its own thread: the news source is slow and rate-limited
    if items:
        for e in items: EVENTS[e["id"]] = e
        FEED.update(t=time.time(), items=items)
    return FEED["items"]

def live_for(company):
    sites = engine.companies()[company]["sites"]; out = []
    for e in FEED["items"]:
        n = _in_reach(e, sites)
        if n > 0 or (e.get("magnitude") or 0) >= 6:
            out.append({**e, "sites_in_reach": n, "note": f"{n} supplier sites in reach"})
    out.sort(key=lambda e: -e["time"])
    return out[:80]

def build_overview(company):
    """Assess every live event that reaches this company's sites (once each), then assemble the global picture."""
    sites = engine.companies()[company]["sites"]; rows = []
    tot = {"events": 0, "alert": 0, "review": 0, "suppliers_alert": 0, "decisions": 0, "ms": 0, "cost_usd": 0.0}
    alert_suppliers = set()
    for e in live_for(company):
        if e["sites_in_reach"] == 0: continue
        key = (company, e["id"])
        if key not in LAST:
            try: LAST[key] = engine.assess({k: v for k, v in e.items() if k not in ("sites_in_reach", "note")}, sites)
            except Exception as ex: print("watch assess failed", e["id"], str(ex)[:120]); continue
        r = LAST[key]; x = r["exposure"]; n = x["suppliers_alert"]
        rows.append({"event": {**r["event"], "sites_in_reach": e["sites_in_reach"], "note": e["note"]}, "reach_km": r["reach_km"], "exposure": x,
                     "headline": (f"{n} supplier{'s' if n != 1 else ''} on alert" if n else f"{x['review']} in review" if x["review"] else "all clear"),
                     "impacts": [i for i in r["impacts"] if i["tier"] != "clear"][:40]})
        tot["events"] += 1; tot["alert"] += x["alert"]; tot["review"] += x["review"]
        for k in ("decisions", "ms", "cost_usd"): tot[k] += r["drex"][k]
        alert_suppliers |= {i["supplier"] for i in r["impacts"] if i["tier"] == "alert"}
    tot["suppliers_alert"] = len(alert_suppliers); tot["cost_usd"] = round(tot["cost_usd"], 6)
    rows.sort(key=lambda r: (-r["exposure"]["alert"], -r["exposure"]["review"], -r["event"]["time"]))
    OVERVIEW[company] = {"updated": int(time.time() * 1000), "warming": False, "events": rows, "totals": tot}

def news_loop():
    while news:
        try:
            got = [e for e in news.fetch() if e.get("lat") is not None and e.get("kind") in NEWS_KINDS]
            if got:
                FEED["news"] = got; FEED["t"] = 0          # make the next feed refresh pick the news up
        except Exception as e: print("news failed:", e)
        time.sleep(600)

def watcher():
    while True:
        try:
            refresh_feeds()
            for company in list(engine.companies()): build_overview(company)
        except Exception as e: print("watcher error:", e)
        time.sleep(45)

for s in SCENARIOS: EVENTS[s["id"]] = s
try:
    _snap = json.load(open(D("cache", "feeds.json"))); FEED["usgs"] = _snap.get("usgs", []); FEED["gdacs"] = _snap.get("gdacs", [])
except Exception: pass

@app.get("/")
def index(): return send_from_directory(app.static_folder, "index.html")

@app.get("/landing")
def landing(): return send_from_directory(app.static_folder, "landing.html")

@app.get("/api/state")
def state():
    cs = engine.companies(); key = co(); c = cs[key]
    sites = [{k: s.get(k) for k in ("id", "supplier", "country", "region", "lat", "lon", "page", "address", "precision")} for s in c["sites"]]
    return jsonify({"name": CFG["name"], "tagline": CFG["tagline"], "company": c["name"], "company_key": key, "document": c["document"], "sites": sites,
                    "companies": [{"key": k, "name": v["name"], "fictional": v["fictional"], "document": v["document"],
                                   "counts": {"suppliers": len({s["supplier"] for s in v["sites"]}), "sites": len(v["sites"])}} for k, v in cs.items()],
                    "counts": {"suppliers": len({s["supplier"] for s in sites}), "sites": len(sites)}, "thresholds": engine.THRESH,
                    "claude_enabled": bool(claude_race and claude_race.available())})

def _pub(ev): return {k: v for k, v in ev.items() if k not in ("reported", "sites_in_reach", "note")}

@app.get("/api/events")
def events():
    key = co()
    return jsonify({"live": live_for(key), "scenarios": [_pub(s) for s in SCENARIOS]})

@app.get("/api/overview")
def overview():
    return jsonify(OVERVIEW.get(co()) or {"updated": int(time.time() * 1000), "warming": True, "events": [],
                                          "totals": {"events": 0, "alert": 0, "review": 0, "suppliers_alert": 0, "decisions": 0, "ms": 0, "cost_usd": 0}})

@app.post("/api/assess")
def assess():
    try:
        eid = (request.get_json(silent=True) or {}).get("event_id"); key = co()
        if eid not in EVENTS: refresh_feeds(force=True)
        ev = EVENTS.get(eid)
        if not ev: return jsonify({"error": "unknown event"}), 404
        if ev.get("lat") is None: return jsonify({"error": "this event has no location"}), 400
        r = engine.assess(_pub(ev), engine.companies()[key]["sites"]); LAST[(key, eid)] = r
        if (key, eid) in BRIEFS and BRIEFS[(key, eid)].get("alert") != r["exposure"]["alert"]: BRIEFS.pop((key, eid))
        return jsonify(r)
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

def _deepinfra(prompt):
    req = urllib.request.Request("https://api.deepinfra.com/v1/openai/chat/completions", data=json.dumps({
        "model": "Qwen/Qwen3.8-27B", "max_tokens": 700, "temperature": 0.3, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": "Bearer " + drex.ENV.get("DEEPINFRA_API_KEY", ""), "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=90))["choices"][0]["message"]["content"]

def _intern(prompt, timeout=120):
    exe = shutil.which("intern") or os.path.expanduser("~/.local/bin/intern")
    if not os.path.exists(exe): return None
    env = dict(os.environ, NACE_API_KEY=drex.KEY, DEEPINFRA_API_KEY=drex.ENV.get("DEEPINFRA_API_KEY", ""))
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([exe, "-p", "--system1", "drex-latest", "--system2", "deepinfra/Qwen/Qwen3.8-27B", "--no-session", "--no-memory",
                            "--no-context-files", "--no-extensions", "--max-turns", "1", "--cwd", tmp, prompt],
                           capture_output=True, text=True, timeout=timeout, env=env, cwd=tmp)
    out = re.sub(r"\n?\[\d+ turns?,[^\]]*\]\s*$", "", r.stdout.strip()).strip()      # drop Intern's "[1 turns, N in / M out tokens]" footer
    return out if r.returncode == 0 and out else None

BRIEFS = {}      # event_id -> brief already written by Intern (past incidents are written in the background at startup)

def write_brief(key, intern_timeout):
        r = LAST[key]; company_name = engine.companies()[key[0]]["name"]
        t = time.time(); ev = r["event"]; top = r["impacts"][:10]
        lines = [f"- {x['supplier']} ({x['makes'] or x['category']}), {x['region']}, {x['country']}: {round(x['distance_km'])} km away, "
                 f"{round(x['p_disrupted']*100)}% chance of at least a day's disruption, most likely {x['severity']['label']}, suggested step: {x['action']['choice'].replace('_', ' ')}; supplier list p.{x['page']}"
                 for x in top]
        prompt = ("Write an incident brief for a supply-chain duty manager at " + company_name + ". At most 130 words, plain text, no markdown, no headings. "
                  "First sentence: what happened and how exposed the supply chain is. Then the three suppliers to call first and why. "
                  "End with one sentence on what is uncertain (site locations are region-level, probabilities are model estimates). "
                  "Use only the facts below; do not invent numbers. Do not edit files or run commands; reply with the brief only.\n\n"
                  f"Event: {ev['title']} ({ev['kind']}), {time.strftime('%d %b %Y %H:%M UTC', time.gmtime(ev['time']/1000))}.\n"
                  f"Supplier sites assessed: {r['candidates']} within {r['reach_km']} km. On alert: {r['exposure']['alert']} sites at {r['exposure']['suppliers_alert']} suppliers; "
                  f"needs review: {r['exposure']['review']}; clear: {r['exposure']['clear']}.\nHighest-risk sites:\n" + "\n".join(lines))
        text, eng = None, "intern"
        try: text = _intern(prompt, intern_timeout)
        except Exception as e: print("intern did not finish in time, using DeepInfra directly:", type(e).__name__)
        if not text: text, eng = _deepinfra(prompt), "deepinfra"
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        return {"text": text, "engine": eng, "secs": round(time.time() - t, 1), "alert": r["exposure"]["alert"]}

@app.post("/api/brief")
def brief():
    try:
        key = (co(), (request.get_json(silent=True) or {}).get("event_id"))
        if key not in LAST: return jsonify({"error": "assess this event first"}), 400
        if key in BRIEFS: return jsonify(BRIEFS[key])
        b = write_brief(key, intern_timeout=40)          # Intern can take a minute; after 40 s fall back to the worker model directly
        if b["engine"] == "intern": BRIEFS[key] = b
        return jsonify(b)
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

@app.post("/api/race")
def race():
    try:
        body = request.get_json(silent=True) or {}; key = (co(), body.get("event_id")); r = LAST.get(key)
        if not r: return jsonify({"error": "assess this event first"}), 400
        if key in RACES and not body.get("force"): return jsonify(RACES[key])
        sites = engine.companies()[key[0]]["sites"]
        ids = [x["site_id"] for x in r["impacts"]]
        pick = ids[:: max(1, len(ids) // 12)][:12]            # a spread across the whole risk range, not just the top
        cands = [c for c in engine.candidates(r["event"], sites)[0] if c["id"] in pick]
        st = engine.build_state(r["event"], cands); refs = [f"S{i+1}" for i in range(len(cands))]
        t = time.time(); d = drex.systemone(st, engine.questions(len(cands)), model=engine.MODEL); dms = round((time.time() - t) * 1000)
        dcost = d["usage"]["input_tokens"] * engine.PRICE_PER_M_INPUT / 1e6
        out = {"sample": len(cands), "drex": {"model": d.get("model"), "ms": dms, "cost_usd": round(dcost, 6), "decisions": 3 * len(cands)},
               "claude": None, "speedup": None, "cheaper": None}
        if claude_race and claude_race.available():
            c = claude_race.decide(st, refs)
            agree = [(d["answers"][f"d_{ref}"]["noul"] >= 0.5) == (float(c["answers"].get(ref, {}).get("p_disrupted", 0)) >= 0.5) for ref in refs if ref in c["answers"]]
            out["claude"] = {"model": c["model"], "ms": c["ms"], "cost_usd": round(c["cost_usd"], 6), "decisions": 3 * len(cands),
                             "agreement": sum(agree) / len(agree) if agree else None}
            out["speedup"] = round(c["ms"] / max(dms, 1), 1); out["cheaper"] = round(c["cost_usd"] / max(dcost, 1e-9), 1)
            RACES[key] = out
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

BENCH = {"scenarios": [], "totals": {"decisions": 0, "ms": 0, "cost_usd": 0}}
def _match(names, supplier):
    s = supplier.lower(); return any(n.lower() in s or s in n.lower() for n in names)
def run_benchmark():
    out, tot = [], {"decisions": 0, "ms": 0, "cost_usd": 0.0}
    for sc in SCENARIOS:
        if not sc.get("reported"): continue          # past incidents without collected ground truth are shown but not scored
        try:
            r = engine.assess(_pub(sc)); LAST[("apple", sc["id"])] = r
            listed = [c for c in sc["reported"] if c.get("in_supplier_list")]
            here = [c for c in listed if any(_match([c["company"]] + c.get("aliases", []), x["supplier"]) for x in r["impacts"])]
            hit = [c["company"] for c in here if any(x["tier"] == "alert" and _match([c["company"]] + c.get("aliases", []), x["supplier"]) for x in r["impacts"])]
            away = [c["company"] for c in listed if c not in here]; off = [c["company"] for c in sc["reported"] if not c.get("in_supplier_list")]
            out.append({"id": sc["id"], "title": sc["title"], "reported": [c["company"] for c in here], "flagged": hit, "hits": len(hit), "total": len(here),
                        "note": f"Reported = companies that publicly said production was affected and that the supplier list places in the area. "
                                f"{r['exposure']['suppliers_alert']} suppliers were put on alert in total (locations are region-level, so it over-flags)."
                                + (f" No site in the area on the list: {', '.join(away)}." if away else "") + (f" Not on the list at all: {', '.join(off)}." if off else "")})
            for k in ("decisions", "ms", "cost_usd"): tot[k] += r["drex"][k]
        except Exception as e: print("benchmark failed for", sc["id"], e)
    tot["cost_usd"] = round(tot["cost_usd"], 6)
    BENCH.update(scenarios=out, totals=tot)
    json.dump(BENCH, open(D("benchmark.json"), "w"), indent=1)
    for sc in SCENARIOS:
        try:
            k = ("apple", sc["id"])
            if k in LAST and k not in BRIEFS:
                b = write_brief(k, intern_timeout=150)
                if b["engine"] == "intern": BRIEFS[k] = b
                print("brief ready for", sc["id"], b["engine"], b["secs"], "s")
        except Exception as e: print("background brief failed", sc["id"], e)

@app.get("/api/benchmark")
def benchmark(): return jsonify(BENCH)

if __name__ == "__main__":
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=news_loop, daemon=True).start()
    threading.Thread(target=run_benchmark, daemon=True).start()
    print(f"{CFG['name']}: {len(engine.SITES)} sites, http://localhost:5188")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5188)), debug=False, threaded=True)
