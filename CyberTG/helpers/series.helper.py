import re
from DB.database import search_series

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except:
    HAVE_RAPIDFUZZ = False

try:
    from spellchecker import SpellChecker
    SPELL = SpellChecker()
    HAVE_SPELLCHECK = True
except:
    SPELL = None
    HAVE_SPELLCHECK = False


def clean_query(q: str):
    if not q:
        return ""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def spell_correct(text: str):
    if not HAVE_SPELLCHECK or not text:
        return text
    words = text.split()
    fixed = []
    for w in words:
        cw = SPELL.correction(w)
        fixed.append(cw if cw else w)
    return " ".join(fixed)


def fuzzy_match(query, choices, cutoff=70):
    if not HAVE_RAPIDFUZZ or not choices:
        return None

    match = rf_process.extractOne(
        query,
        choices,
        scorer=rf_fuzz.partial_ratio,
        score_cutoff=cutoff
    )
    return match[0] if match else None


def strict_match_only(query, choices):
    q = query.lower()
    for c in choices:
        if q in c.lower():
            return c
    return None


def clean_series_name(name: str) -> str:
    name = re.sub(r"[. _]+", " ", name).strip()
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.title()


def extract_quality(file_name: str) -> str:
    name = (file_name or "").upper()
    if "2160P" in name or "4K" in name:
        return "4K"
    if "1080P" in name:
        return "1080P"
    if "720P" in name:
        return "720P"
    return "480P"


def extract_language(file_name: str) -> str:
    name = (file_name or "").upper()

    langs = {
        "HINDI": "Hindi",
        "ENGLISH": "English",
        "TAMIL": "Tamil",
        "TELUGU": "Telugu",
        "MALAYALAM": "Malayalam"
    }

    found = []
    for k, v in langs.items():
        if k in name:
            found.append(v)

    if not found:
        return "English"

    if len(found) == 1:
        return found[0]

    return "-".join(sorted(found))


def parse_series_info(file_name: str):
    if not file_name:
        return "", 0, 0, "", ""

    match = re.search(r"[sS](\d+)[^\d]*[eE](\d+)", file_name)

    quality = extract_quality(file_name)
    language = extract_language(file_name)

    if not match:
        return clean_series_name(file_name), 0, 0, quality, language

    season = int(match.group(1))
    episode = int(match.group(2))

    left = file_name[:match.start()]
    series = clean_series_name(left)

    return series, season, episode, quality, language


async def resolve_series(query: str):
    query = spell_correct(clean_query(query))

    rows = await search_series(query)
    if not rows:
        return None

    titles = [clean_series_name(x) for x in rows]
    return fuzzy_match(query, titles) or strict_match_only(query, titles)
