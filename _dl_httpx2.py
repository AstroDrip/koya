import json
import ssl
from pathlib import Path

import certifi
import httpx

out = Path.home() / "AppData/Local/Temp/sarvam-wheels"
out.mkdir(parents=True, exist_ok=True)

ctx = ssl.create_default_context(cafile=certifi.where())
ctx.load_default_certs()

packages = ["httpx2", "sarvam-mcp", "fastmcp", "fastmcp-slim"]

with httpx.Client(verify=ctx, timeout=120.0, follow_redirects=True, http2=False) as client:
    for name in packages:
        meta = client.get(f"https://pypi.org/pypi/{name}/json").json()
        version = meta["info"]["version"]
        files = meta["releases"][version]
        wheels = [f for f in files if f["packagetype"] == "bdist_wheel" and f["filename"].endswith(".whl")]
        chosen = wheels[0] if wheels else files[0]
        dest = out / chosen["filename"]
        if dest.exists() and dest.stat().st_size > 0:
            print("exists", dest.name, dest.stat().st_size)
            continue
        print("downloading", chosen["url"])
        r = client.get(chosen["url"])
        r.raise_for_status()
        dest.write_bytes(r.content)
        print("saved", dest.name, dest.stat().st_size)
