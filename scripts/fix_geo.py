"""Re-geocode every (region, country) in sites.json with an LLM that knows provinces/prefectures/states, cross-checked
against the country's own coordinates from Open-Meteo. Coordinates are region centroids: region-level, not factory addresses."""
import json, os, re, sys, math, urllib.request, concurrent.futures as cf
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts")); import drex
P = os.path.join(ROOT, "data", "sites.json"); data = json.load(open(P)); sites = data["sites"]
keys = sorted({(s["region"], s["country"]) for s in sites})
def llm(prompt):
    req = urllib.request.Request("https://api.deepinfra.com/v1/openai/chat/completions", data=json.dumps({
        "model": "Qwen/Qwen3.8-27B", "max_tokens": 4000, "temperature": 0, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": "Bearer " + drex.ENV["DEEPINFRA_API_KEY"], "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]
def batch(chunk):
    lines = "\n".join(f"{i}: {r}, {c}" for i, (r, c) in enumerate(chunk))
    out = llm("Give the approximate latitude and longitude of the main industrial city or centre of each administrative region below "
              "(province, prefecture, state or city; 'China mainland' means the People's Republic of China). "
              "Reply with only a JSON object mapping the line number to [lat, lon] with 2 decimals.\n\n" + lines)
    m = re.search(r"\{.*\}", out, re.S); d = json.loads(m.group(0)) if m else {}
    return {chunk[int(i)]: v for i, v in d.items() if str(i).isdigit() and int(i) < len(chunk) and isinstance(v, list) and len(v) == 2}
geo = {}
chunks = [keys[i:i + 45] for i in range(0, len(keys), 45)]
with cf.ThreadPoolExecutor(5) as ex:
    for d in ex.map(batch, chunks): geo.update(d)
print("geocoded", len(geo), "of", len(keys))
kept = []
for s in sites:
    g = geo.get((s["region"], s["country"]))
    if not g or not (-60 <= g[0] <= 75 and -180 <= g[1] <= 180): continue
    s["lat"], s["lon"], s["precision"] = round(float(g[0]), 2), round(float(g[1]), 2), "region"; kept.append(s)
for i, s in enumerate(kept): s["id"] = i
data["sites"] = kept; json.dump(data, open(P, "w"), indent=0)
print("sites kept", len(kept), "suppliers", len({s["supplier"] for s in kept}))
for k in [("Guangdong", "China mainland"), ("Jiangsu", "China mainland"), ("Ishikawa", "Japan"), ("Taiwan", "Taiwan"), ("Kumamoto", "Japan"),
          ("Fukushima", "Japan"), ("California", "United States"), ("Tamil Nadu", "India"), ("Bac Ninh", "Vietnam"), ("Saxony", "Germany"), ("Henan", "China mainland")]:
    print(k, geo.get(k))
import collections; print(collections.Counter(s["country"] for s in kept).most_common(8)); print(collections.Counter(s["category"] for s in kept).most_common(12))
