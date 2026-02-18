import re
import base64

PRIVATE_LINK_PATTERN = r"https://t\.me/c/(\d+)/(\d+)"
PUBLIC_LINK_PATTERN = r"https://t\.me/([A-Za-z0-9_]+)/(\d+)"
JOINCHAT_PATTERN = r"https://t\.me/\+([A-Za-z0-9_-]+)"

def extract_links(links, client=None):

    def parse_link(link):
        match = re.match(PRIVATE_LINK_PATTERN, link)
        if match:
            cid, mid = match.groups()
            return int(f"-100{cid}"), int(mid)

        match = re.match(PUBLIC_LINK_PATTERN, link)
        if match:
            username, mid = match.groups()
            chat = client.get_chat(username)
            return chat.id, int(mid)

        raise ValueError("Invalid link")

    if len(links) == 1:
        chat, last = parse_link(links[0])
        return chat, 2, last

    chat1, start = parse_link(links[0])
    chat2, last = parse_link(links[1])

    return chat1, start, last


def encode_payload(series, season, lang, quality, uid):
    raw = f"{uid}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"send_{b64}"


def decode_payload(payload):
    b64 = payload.replace("send_", "")
    pad = "=" * (-len(b64) % 4)
    return base64.urlsafe_b64decode(b64 + pad).decode().split("|")
