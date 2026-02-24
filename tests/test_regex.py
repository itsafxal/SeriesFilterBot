import sys
import os

# Add current directory to path so we can import utils
sys.path.append(os.getcwd())

from utils.cyber_series import parse_series_info, _normalize_title, clean_query

def test():
    test_cases = [
        "The.Mandalorian.S01E02.1080p.mkv",
        "The Mandalorian Season 1 Episode 2 720p.mkv",
        "The Mandalorian 1x02 480p.avi",
        "The.Mandalorian.E10.1080p.mkv",
        "The.Mandalorian.EP10.1080p.mkv",
        "The.Mandalorian.2019.S01E01.1080p.mkv",
        "The.Mandalorian.Combined.1080p.mkv",
        "The.Mandalorian.S01.EP02.1080p.mkv",
        "The.Mandalorian.S01E01E02.1080p.mkv",
        "The.Mandalorian.S01.1080p.mkv",
    ]

    print(f"{'Original':<50} | {'Normalized':<20} | {'S':<2} | {'E':<2}")
    print("-" * 85)
    for tc in test_cases:
        norm, s, e, q, l = parse_series_info(tc)
        print(f"{tc:<50} | {norm:<20} | {s:<2} | {e:<2}")

    print("\nTesting clean_query:")
    queries = [
        "The Mandalorian S01",
        "The Mandalorian Season 1",
        "The Mandalorian 1x01",
    ]
    for q in queries:
        cleaned = clean_query(q)
        print(f"'{q}' -> '{cleaned}'")

if __name__ == "__main__":
    test()
