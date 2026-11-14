"""Claude side of the Drex-vs-Claude race: the same supplier-disruption decisions Drex makes, in one Messages API call.

Raw HTTP on purpose (stdlib only, no SDK install). Request shape, model id and prices come from the bundled
`claude-api` skill (model table cached 2026-11-03):
  - POST https://api.anthropic.com/v1/messages, headers x-api-key + anthropic-version: 2026-10-21
  - claude-opus-5-5: thinking cannot be disabled and sampling params (temperature) are rejected, so depth is
    set with output_config.effort = "low"; structured output goes in output_config.format
  - refusal fallbacks are opted in (beta server-side-fallback-2026-07-01, fallbacks: "default"); the response's
    `model` says which model actually answered, and cost is priced for that model
"""
import json, os, re, sys, time, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drex

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5-5"

# USD per 1M tokens (input, output). Source: claude-api skill, "Current Models (cached: 2026-11-03)".
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.00, 40.00),
}
SEVERITY = ["no impact", "inspection pause (hours)", "partial shutdown (days)", "extended shutdown (weeks)"]
ACTIONS = ["monitor", "contact_supplier", "activate_backup"]


def _key():
    """ANTHROPIC_API_KEY from the environment, else from the repo's .env (re-read so a key pasted later is picked up)."""
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k: return k.strip()
    try: env = drex._env()
    except Exception: env = drex.ENV
    return (env.get("ANTHROPIC_API_KEY") or "").strip()


def available() -> bool:
    return bool(_key())


def _price(model):
    for name in sorted(PRICES, key=len, reverse=True):          # longest id first: "claude-opus-5-5" before "claude-opus-5"
        if model.startswith(name): return PRICES[name]
    return PRICES[DEFAULT_MODEL]


def _schema(refs):
    one = {"type": "object", "additionalProperties": False, "required": ["p_disrupted", "severity", "action"],
           "properties": {"p_disrupted": {"type": "number"},
                          "severity": {"type": "integer", "enum": [0, 1, 2, 3]},
                          "action": {"type": "string", "enum": ACTIONS}}}
    return {"type": "object", "additionalProperties": False, "required": list(refs), "properties": {r: one for r in refs}}


def _prompt(state, refs):
    return (
        "You assess supply-chain disruption. Below is one event and a list of supplier sites, as JSON.\n\n"
        + json.dumps(state, ensure_ascii=False, indent=1) + "\n\n"
        "For each of these site refs: " + ", ".join(refs) + "\n"
        "decide three things:\n"
        "- p_disrupted: probability from 0 to 1 that the site stops or slows production for at least one day because of this event.\n"
        "- severity: integer 0 to 3, where " + "; ".join(f"{i} = {s}" for i, s in enumerate(SEVERITY)) + ".\n"
        "- action: one of " + ", ".join(ACTIONS) + ".\n\n"
        "Reply with strict JSON only: one object keyed by site ref, each value "
        '{"p_disrupted": number, "severity": integer, "action": string}. No prose, no code fences.'
    )


def _post(body, key, beta=None):
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "Content-Type": "application/json"}
    if beta: headers["anthropic-beta"] = beta
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=headers)
    try:
        return json.load(urllib.request.urlopen(req, timeout=240)), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try: msg = json.loads(raw)["error"]["message"]
        except Exception: msg = raw[:400]
        return None, (e.code, msg)


def _parse(text):
    """Strip code fences and take the outermost JSON object."""
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i: raise RuntimeError("Claude did not return a JSON object: " + text[:200])
    return json.loads(t[i:j + 1])


def decide(state: dict, site_refs: list, model: str = DEFAULT_MODEL) -> dict:
    key = _key()
    if not key: raise RuntimeError("ANTHROPIC_API_KEY is not set")
    refs = [str(r) for r in site_refs]
    base = {"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": _prompt(state, refs)}]}
    # Most capable request first; if the API rejects an optional feature with a 400, drop that feature and retry.
    attempts = [
        (dict(base, output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(refs)}}, fallbacks="default"), FALLBACK_BETA),
        (dict(base, output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(refs)}}), None),
        (dict(base, output_config={"effort": "low"}), None),
    ]
    t0 = time.time(); resp = err = None
    for body, beta in attempts:
        for retry in range(3):
            resp, err = _post(body, key, beta)
            if err and err[0] in (429, 500, 502, 503, 529) and retry < 2: time.sleep(2 * (retry + 1)); continue
            break
        if resp is not None or err[0] != 400: break            # only a 400 (invalid request) moves on to a simpler request
    if resp is None: raise RuntimeError(f"Claude API error {err[0]}: {err[1]}")
    ms = round((time.time() - t0) * 1000)
    if resp.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined the request (stop_reason: refusal)")
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    if resp.get("stop_reason") == "max_tokens" and not text.strip():
        raise RuntimeError("Claude hit max_tokens before answering")
    raw = _parse(text)
    answers = {}
    for r in refs:
        a = raw.get(r)
        if not isinstance(a, dict): continue
        try:
            p = min(1.0, max(0.0, float(a.get("p_disrupted"))))
            sev = min(3, max(0, int(round(float(a.get("severity"))))))
        except (TypeError, ValueError): continue
        act = a.get("action") if a.get("action") in ACTIONS else "monitor"
        answers[r] = {"p_disrupted": p, "severity": sev, "action": act}
    u = resp.get("usage") or {}
    tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    tout = u.get("output_tokens") or 0
    used = resp.get("model") or model
    pin, pout = _price(used)
    return {"model": used, "ms": ms, "input_tokens": tin, "output_tokens": tout,
            "cost_usd": round((tin * pin + tout * pout) / 1e6, 6), "answers": answers}


if __name__ == "__main__":
    if not available():
        print("no key set")
    else:
        demo = {"event": {"kind": "earthquake", "title": "M 7.4 - 15 km S of Hualien City, Taiwan", "magnitude": 7.4, "depth_km": 40},
                "sites": [{"ref": "S1", "supplier": "Example Fab Co", "makes": "logic chips", "category": "semiconductor fab",
                           "region": "Hsinchu", "country": "Taiwan", "distance_km": 110, "shaking_intensity_mmi": 5.8},
                          {"ref": "S2", "supplier": "Example Cable Co", "makes": "cables", "category": "connectors/cables",
                           "region": "Guangdong", "country": "China mainland", "distance_km": 870, "shaking_intensity_mmi": 2.1}]}
        print(json.dumps(decide(demo, ["S1", "S2"]), indent=1))
