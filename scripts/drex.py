"""Minimal Drex client (stdlib only). Reads NACE_API_KEY / DREX_API_KEY from .env."""
import base64, io, json, os, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _env():
    kv = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for l in open(p):
            if "=" in l and not l.lstrip().startswith("#"):
                k, v = l.strip().split("=", 1); kv[k] = v.strip().strip("\"'")
    return kv
ENV = _env()
KEY = os.environ.get("NACE_API_KEY") or ENV.get("NACE_API_KEY") or ENV.get("DREX_API_KEY")
BASE = "https://console.nace.ai/v1"

def img_b64(path, max_side=1100, quality=80):
    """Downscale and re-encode so two images fit well under the 4 MiB body limit."""
    from PIL import Image
    im = Image.open(io.BytesIO(path) if isinstance(path, (bytes, bytearray)) else path).convert("RGB")
    w, h = im.size
    # very tall pages: keep the top part (what a visitor sees first), cap aspect at 1:2.2
    if h > 2.2 * w: im = im.crop((0, 0, w, int(2.2 * w))); w, h = im.size
    s = max_side / max(w, h)
    if s < 1: im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def systemone(state, questions, images=None, model="drex-max", retries=5):
    body = {"model": model, "state": state, "questions": questions}
    if images: body["images"] = images
    data = json.dumps(body).encode()
    for a in range(retries):
        req = urllib.request.Request(BASE + "/systemone", data=data, headers={
            "Authorization": "Bearer " + KEY, "Content-Type": "application/json", "User-Agent": "hackathon-demo/0.1 (python-urllib)", "Accept": "application/json"})
        t = time.time()
        try:
            r = json.load(urllib.request.urlopen(req, timeout=120))
            r["_wall_ms"] = round((time.time() - t) * 1000)
            return r
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:400]
            if (e.code in (429, 529, 500, 502, 503) or (e.code == 403 and 'challenge' in msg.lower())) and a < retries - 1:
                wait = float(e.headers.get("retry-after-ms", 0)) / 1000 or float(e.headers.get("retry-after", 0)) or 2 * (a + 1)
                time.sleep(min(wait, 20)); continue
            raise RuntimeError(f"HTTP {e.code}: {msg}")
        except Exception as e:
            if a < retries - 1: time.sleep(2 * (a + 1)); continue
            raise

def models():
    req = urllib.request.Request(BASE + "/models", headers={"Authorization": "Bearer " + KEY, "User-Agent": "hackathon-demo/0.1 (python-urllib)", "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))
