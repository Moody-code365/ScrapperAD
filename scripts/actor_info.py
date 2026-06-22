"""scripts/actor_info.py — печатает входную схему актора (без запуска, бесплатно)."""
import json
from apify_client import ApifyClient
from config.settings import settings

def to_dict(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj) if obj else {}

c = ApifyClient(settings.apify_token)
actor = to_dict(c.actor("apify/facebook-ads-scraper").get())
tagged = actor.get("taggedBuilds") or {}
build_id = (tagged.get("latest") or {}).get("buildId") or (tagged.get("latest") or {}).get("build_id")
schema = None
if build_id:
    build = to_dict(c.build(build_id).get())
    schema = build.get("inputSchema") or build.get("input_schema")

with open("data/_actor_schema.json", "w", encoding="utf-8") as f:
    json.dump({"build": build_id, "inputSchema": schema}, f, ensure_ascii=False, indent=2, default=str)
print("build:", build_id, "| schema saved:", bool(schema))
