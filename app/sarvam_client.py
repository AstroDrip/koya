from .config import SARVAM_API_KEY

class SarvamNotConfigured(RuntimeError):
    pass

def generate_smoke_test_reply(prompt: str) -> str:
    if not SARVAM_API_KEY:
        raise SarvamNotConfigured("SARVAM_API_KEY is not configured in the server environment.")
    from sarvamai import SarvamAI

    client = SarvamAI(api_subscription_key=SARVAM_API_KEY)
    response = client.chat.completions(
        model="sarvam-105b-conversations",
        messages=[
            {"role": "system", "content": "You are a concise Malayalam-capable assistant. This is an API connectivity test, not a real MR CARE customer session."},
            {"role": "user", "content": prompt[:2000]},
        ],
    )
    content = response.choices[0].message.content
    return content if isinstance(content, str) else str(content)
