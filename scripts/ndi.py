"""NDI (Nace Document Intelligence) helper: upload a file, parse it into text blocks with locations."""
import json, os, sys, time, uuid, urllib.request, urllib.error, mimetypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drex
def _post(url, body, headers=None, timeout=90):
    h = {"Authorization": "Bearer " + drex.KEY, "Content-Type": "application/json", "User-Agent": "hackathon-demo/0.1 (python-urllib)", "Accept": "application/json"}; h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    for a in range(4):
        try: return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:400]
            if e.code in (403, 429, 500, 502, 503, 529) and a < 3: time.sleep(3 * (a + 1)); continue   # edge protection / rate limit: wait and retry
            raise RuntimeError(f"NDI HTTP {e.code}: {msg}")
def upload(path=None, data=None, name=None):
    name = name or os.path.basename(path)
    data = data if data is not None else open(path, "rb").read()
    g = _post(drex.BASE + "/documents/upload-grants", {"path": name})
    b = "----ndi" + uuid.uuid4().hex
    ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(name)}\"\r\nContent-Type: {ct}\r\n\r\n").encode() + data + \
           (f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n\r\n" + json.dumps({"path": name}) + f"\r\n--{b}--\r\n").encode()
    req = urllib.request.Request(g["upload_url"], data=body, headers={"X-Upload-Token": g["token"], "Content-Type": "multipart/form-data; boundary=" + b, "User-Agent": "hackathon-demo/0.1 (python-urllib)"})
    try: r = json.load(urllib.request.urlopen(req, timeout=90))
    except urllib.error.HTTPError as e: raise RuntimeError(f"NDI upload HTTP {e.code}: {e.read().decode()[:400]}")
    return g["workspace_id"], r["result"]["file"]["file_id"]
def parse(path=None, data=None, name=None, wait=60):
    ws, fid = upload(path, data, name)
    return _post(drex.BASE + f"/documents/parse?wait_seconds={wait}", {"source": {"type": "workspace_file", "workspace_id": ws, "file_id": fid},
                 "output": {"formats": ["markdown", "blocks"]}}, timeout=wait + 30)
if __name__ == "__main__":
    t = time.time(); r = parse(sys.argv[1]); print(round(time.time() - t, 1), "s"); print(json.dumps(r)[:3000])
