import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.loyverse.com/v1.0"
YANGON_TZ = timezone(timedelta(hours=6, minutes=30))


def fetch_all_pages(endpoint: str, data_key: str, token: str) -> list:
    """Fetch all paginated records from a Loyverse API v1.0 endpoint."""
    records = []
    cursor = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    while True:
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor

        url = f"{BASE_URL}/{endpoint}?{urlencode(params)}"
        req = Request(url, headers=headers, method="GET")

        try:
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Loyverse API HTTP {e.code} on {endpoint}: {body}") from e
        except URLError as e:
            raise RuntimeError(f"Network error on {endpoint}: {e.reason}") from e

        batch = payload.get(data_key, [])
        records.extend(batch)

        cursor = payload.get("cursor")
        if not cursor:
            break

    return records


def format_price(value) -> str:
    """Format numeric price cleanly as text for custom_items.price."""
    if value is None:
        return "0"
    num = float(value)
    return str(int(num)) if num.is_integer() else str(num)


def build_supabase_rows(all_items: list, all_inventory: list, now_iso: str) -> list:
    """Map Loyverse items + inventory to custom_items schema (keyed by sku)."""
    stock_by_variant = {}
    for inv in all_inventory:
        vid = inv.get("variant_id")
        if vid:
            stock_by_variant[vid] = stock_by_variant.get(vid, 0) + (inv.get("in_stock") or 0)

    rows_by_sku = {}
    for item in all_items:
        if item.get("deleted_at"):
            continue
        item_name = item.get("item_name") or ""
        image_url = item.get("image_url") or ""
        created_at = item.get("created_at") or now_iso

        for variant in item.get("variants", []):
            if variant.get("deleted_at"):
                continue
            vid = variant.get("variant_id")
            if not vid:
                continue

            sku = str(variant.get("sku") or vid).strip()

            # Prefer store-level price if set, otherwise fallback to default_price
            price_val = variant.get("default_price")
            for st in variant.get("stores", []):
                if st.get("price") is not None:
                    price_val = st.get("price")
                    break

            rows_by_sku[sku] = {
                "sku": sku,
                "item_name": item_name,
                "price": format_price(price_val),
                "image_url": image_url,
                "stock": stock_by_variant.get(vid, 0),
                "item_created_at": created_at,
            }

    return list(rows_by_sku.values())


def upsert_to_supabase(
    supabase_url: str, service_key: str, table: str, rows: list, batch_size: int = 200
) -> None:
    """Upsert rows into Supabase via PostgREST using sku as conflict key."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{table}?on_conflict=sku"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        data = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
        req = Request(endpoint, data=data, headers=headers, method="POST")

        try:
            with urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201, 204):
                    raise RuntimeError(f"Unexpected Supabase status {resp.status}")
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase HTTP {e.code} on batch {i // batch_size + 1}: {body}") from e
        except URLError as e:
            raise RuntimeError(f"Supabase network error: {e.reason}") from e

    print(f"Upserted {len(rows)} rows into Supabase table '{table}'.")


def main() -> None:
    token = os.environ.get("LOYVERSE_TOKEN", "").strip()
    if not token:
        raise SystemExit("Error: LOYVERSE_TOKEN environment variable is missing.")

    output_file = os.environ.get("OUTPUT_FILE", "loyverse_data.json").strip()

    print("Fetching items (including prices and image_url)...")
    all_items = fetch_all_pages("items", "items", token)
    print(f"Fetched {len(all_items)} items.")

    print("Fetching inventory levels...")
    all_inventory = fetch_all_pages("inventory", "inventory_levels", token)
    print(f"Fetched {len(all_inventory)} inventory records.")

    now_dt = datetime.now(YANGON_TZ)
    now_mmt = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    now_iso = now_dt.isoformat()

    output_data = {
        "items": {
            "items": all_items,
        },
        "inventory": {
            "inventory_levels": all_inventory,
        },
        "last_updated": now_mmt,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print(f"Saved {output_file} at {now_mmt} (+06:30).")

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    supabase_table = os.environ.get("SUPABASE_TABLE", "custom_items").strip()

    if supabase_url and supabase_key:
        rows = build_supabase_rows(all_items, all_inventory, now_iso)
        upsert_to_supabase(supabase_url, supabase_key, supabase_table, rows)
    else:
        print("Skipped Supabase sync (SUPABASE_URL or SUPABASE_SERVICE_KEY not set).")


if __name__ == "__main__":
    main()