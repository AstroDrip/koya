import os
import ssl
from pathlib import Path

import certifi
import httpx
from sarvamai import SarvamAI

env_path = Path(__file__).with_name(".env")
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

api_key = os.environ.get("SARVAM_API_KEY")
if not api_key:
    raise SystemExit("SARVAM_API_KEY is missing. Paste it into .env and try again.")

ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl_context.load_default_certs()
client = SarvamAI(
    api_subscription_key=api_key,
    httpx_client=httpx.Client(verify=ssl_context, timeout=60.0, follow_redirects=True),
)
response = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[{"role": "user", "content": "Say hello in one short sentence."}],
)
print(response.choices[0].message.content)
