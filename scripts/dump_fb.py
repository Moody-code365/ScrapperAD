"""
scripts/dump_fb.py — диагностика: дамп сырого ответа Apify FB Ads актора в JSON.
Запуск: python -m scripts.dump_fb "<facebook_page_or_url>" [max_ads]
"""
import json
import sys

from apify_client import ApifyClient
from config.settings import settings

FACEBOOK_ADS_ACTOR = "apify/facebook-ads-scraper"


def main():
    page = sys.argv[1]
    max_ads = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    url = page if page.startswith("http") else f"https://www.facebook.com/{page}"

    client = ApifyClient(settings.apify_token)
    run = client.actor(FACEBOOK_ADS_ACTOR).call(run_input={
        "startUrls": [{"url": url}],
        "resultsLimit": max_ads,   # правильное поле лимита (не maxAds!)
    })
    dataset_id = getattr(run, "default_dataset_id", None) or run["defaultDatasetId"]

    items = list(client.dataset(dataset_id).iterate_items())
    with open("data/_fb_dump.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(items)} items to data/_fb_dump.json")


if __name__ == "__main__":
    main()
