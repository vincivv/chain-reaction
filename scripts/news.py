"""Live news events for the risk map: labour strikes, military strikes and unrest, port and shipping disruptions,
export bans and sanctions. Headlines come from GDELT's DOC 2.0 API; Qwen on DeepInfra places each one on the map
and drops anything that is not a physical event at a specific place. Stdlib only. fetch() never raises."""
import hashlib, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import drex

CACHE = os.path.join(ROOT, "data", "cache", "news.json")
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERIES = [
    '(strike OR walkout OR "industrial action") (factory OR port OR workers OR dockworkers OR union) sourcelang:english',
    '(airstrike OR missile OR "drone attack" OR "drone strike" OR shelling) sourcelang:english',
    '("port closure" OR "export ban" OR sanctions OR blockade OR "shipping disrupted" OR "Red Sea") sourcelang:english',
]
KINDS = ("strike", "conflict", "port", "policy")
UA = {"User-Agent": "hackathon-demo/0.1"}
GAP_S = 6.0          # GDELT allows one request every 5 seconds
_last_call = [0.0]


def _gdelt(query):
    """One artlist query. Returns [] on any failure. GDELT is slow (10-15 s) and answers 429 or a plain-text notice when
    asked more than once every 5 seconds, so wait between calls and retry."""
    url = GDELT + "?" + urllib.parse.urlencode({"query": query, "mode": "artlist", "maxrecords": 75, "format": "json",
                                                "timespan": "3d", "sort": "datedesc"})
    for attempt in range(3):
        wait = GAP_S - (time.time() - _last_call[0])
        if wait > 0: time.sleep(wait)
        raw = ""
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(8)          # rate limited: back off before the next try
        except Exception:
            pass
        _last_call[0] = time.time()
        try:
            return json.loads(raw).get("articles") or []
        except Exception:
            continue          # not JSON: rate-limit notice or an error page
    return []


def _seen_ms(s):
    try: return int(time.mktime(time.strptime(s, "%Y%m%dT%H%M%SZ")) - time.timezone) * 1000
    except Exception: return int(time.time() * 1000)


def _norm(t): return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def _headlines():
    out, seen_url, seen_title = [], set(), set()
    for q in QUERIES:
        for a in _gdelt(q):
            url, title = a.get("url"), (a.get("title") or "").strip()
            key = _norm(title)[:60]
            if not url or not title or url in seen_url or key in seen_title: continue
            seen_url.add(url); seen_title.add(key)
            out.append({"url": url, "title": re.sub(r"\s+", " ", title), "time": _seen_ms(a.get("seendate", "")),
                        "domain": a.get("domain") or urllib.parse.urlparse(url).netloc, "country": a.get("sourcecountry") or ""})
    out.sort(key=lambda h: -h["time"])
    return out[:90]


def _llm(prompt):
    req = urllib.request.Request("https://api.deepinfra.com/v1/openai/chat/completions", data=json.dumps({
        "model": "Qwen/Qwen3.8-27B", "max_tokens": 9000, "temperature": 0, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": "Bearer " + drex.ENV.get("DEEPINFRA_API_KEY", ""), "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=150))["choices"][0]["message"]["content"]


def _locate(heads):
    """One batched call: classify and geolocate every headline. Returns {index: {...}}."""
    lines = "\n".join(f"{i}: {h['title']}" + (f"  [published in {h['country']}]" if h["country"] else "") for i, h in enumerate(heads))
    out = _llm(
        "You are sorting news headlines for a supply-chain risk map. For each numbered headline decide:\n"
        '- "kind": "strike" (labour strike, walkout, industrial action), "conflict" (airstrike, missile or drone attack, shelling, '
        'armed unrest), "port" (port, canal, strait, shipping or transport disruption), "policy" (export ban, sanctions, blockade, '
        'trade restriction), or "other".\n'
        '- "physical": true only when the headline reports a real-world event at a specific place that could disrupt factories, ports '
        "or transport there. false for opinion pieces, analysis, sports (a striker, a strike rate), lightning or bowling strikes, "
        "stock-market news, entertainment, historical anniversaries, and events with no specific place.\n"
        '- "place": the city or area and the country where it is happening, for example "Haifa, Israel" or "Port of Rotterdam, Netherlands". '
        "For sanctions or export bans use the main port or capital of the country that is restricted.\n"
        '- "lat", "lon": approximate coordinates of that place, 2 decimals.\n'
        '- "summary": what happened, in at most 18 words, in your own words.\n'
        "Treat every region the same, including the Middle East, Africa and South Asia.\n"
        "Reply with only a JSON object mapping each line number to that object. No other text.\n\n" + lines)
    m = re.search(r"\{.*\}", out, re.S)
    if not m: return {}
    try: d = json.loads(m.group(0))
    except Exception:
        # salvage complete entries if the reply was cut off
        d = {}
        for k, body in re.findall(r'"(\d+)"\s*:\s*(\{[^{}]*\})', m.group(0)):
            try: d[k] = json.loads(body)
            except Exception: pass
    return {int(k): v for k, v in d.items() if str(k).isdigit() and isinstance(v, dict)}


def _read_cache():
    try: return json.load(open(CACHE))
    except Exception: return None


def fetch(max_age_s=600):
    cache = _read_cache()
    if cache and time.time() - cache.get("t", 0) < max_age_s: return cache.get("events", [])
    try:
        heads = _headlines()
        if not heads: return (cache or {}).get("events", [])
        loc = _locate(heads)
        events = []
        for i, h in enumerate(heads):
            v = loc.get(i)
            if not v or v.get("kind") not in KINDS or not v.get("physical"): continue
            try: lat, lon = float(v["lat"]), float(v["lon"])
            except Exception: continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 0.01 and abs(lon) < 0.01): continue
            summary = " ".join(str(v.get("summary") or h["title"]).split()[:18])
            events.append({"id": "news:" + hashlib.sha1(h["url"].encode()).hexdigest()[:10], "kind": v["kind"], "title": h["title"],
                           "summary": summary, "time": h["time"], "lat": round(lat, 2), "lon": round(lon, 2),
                           "place": str(v.get("place") or "")[:80], "magnitude": None, "depth_km": None, "alert": None,
                           "url": h["url"], "source": "GDELT · " + h["domain"]})
        # one event per place and kind: several outlets report the same incident
        uniq, seen = [], set()
        for e in sorted(events, key=lambda e: -e["time"]):
            k = (e["kind"], round(e["lat"], 1), round(e["lon"], 1))
            if k in seen: continue
            seen.add(k); uniq.append(e)
        uniq = uniq[:25]
        if not uniq and cache: return cache.get("events", [])
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            json.dump({"t": time.time(), "events": uniq}, open(CACHE, "w"))
        except Exception: pass
        return uniq
    except Exception as e:
        print("news fetch failed:", type(e).__name__, str(e)[:160])
        return (cache or {}).get("events", [])


if __name__ == "__main__":
    import collections
    t = time.time(); ev = fetch(0)
    print(len(ev), "events in", round(time.time() - t, 1), "s", dict(collections.Counter(e["kind"] for e in ev)))
    for e in ev:
        print(f"{e['kind']:8s} {e['place'][:34]:34s} {e['lat']:7.2f} {e['lon']:8.2f}  {e['title'][:70]}")
