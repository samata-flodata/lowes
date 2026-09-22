from __future__ import annotations

import html as html_lib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from backend.config import DATA_DIR

MENARDS_DIR = DATA_DIR / "menards"
logger = logging.getLogger("lowes_map_system.menards")


def _store_dir(store_id: str) -> Path:
    return (MENARDS_DIR / str(store_id)).resolve()


def _metadata_path(store_id: str) -> Path:
    return _store_dir(store_id) / "metadata.json"


def _svg_path(store_id: str) -> Path:
    return _store_dir(store_id) / "floor1.svg"


def extract_store_id(store_url: str) -> str:
    if not store_url:
        raise ValueError("Store URL is required")
    parsed = urlparse(store_url)
    query = parse_qs(parsed.query)
    if query.get("store"):
        value = query["store"][0]
        if re.fullmatch(r"\d+", str(value)):
            return str(value)
    match = re.search(r"(?:store|store_id)[=/]?(\d+)", parsed.path + "?" + parsed.query, re.I)
    if match:
        return match.group(1)
    if re.search(r"\d+", parsed.netloc):
        match = re.search(r"(\d+)", parsed.netloc)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract store ID from URL: {store_url}")


def _normalize_address(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?<=\w),(?=\s*[A-Z]{2}\d{5})", ", ", text, flags=re.I)
    text = re.sub(r"\s*([A-Z]{2})(\d{5})\b", r"\1 \2", text, flags=re.I)
    text = re.sub(r"\s*([A-Z]{2})\s+(\d{5})\b", r"\1 \2", text, flags=re.I)
    text = re.sub(r",\s*([A-Z]{2})\s*(\d{5})\b", r", \1 \2", text, flags=re.I)
    return text.replace(" ,", ",").replace(",IA ", ", IA ").replace(",IA", ", IA").strip()


def _decode_initial_store_payload(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = html_lib.unescape(raw)
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            try:
                items = json.loads(text.replace('&quot;', '"').replace('&#34;', '"'))
            except json.JSONDecodeError:
                return []
    else:
        items = [raw]

    if not isinstance(items, list):
        return []
    return items


def parse_initial_store_records(html_text: str) -> List[Dict[str, Any]]:
    if not html_text:
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    meta = soup.find("meta", attrs={"id": "initialStores"})
    payload = meta.get("data-initial-stores") if meta else None
    if not payload:
        return []

    decoded = html_lib.unescape(payload)
    decoded = decoded.replace('&#34;', '"').replace('&quot;', '"')

    try:
        raw_items = json.loads(decoded)
    except json.JSONDecodeError:
        try:
            raw_items = json.loads(decoded.replace('\\"', '"'))
        except json.JSONDecodeError:
            return []

    records: List[Dict[str, Any]] = []
    for item in _decode_initial_store_payload(raw_items):
        if not isinstance(item, dict):
            continue
        raw_store_id = item.get('number')
        if raw_store_id is None:
            raw_store_id = item.get('storeNumber')
        if raw_store_id is None:
            raw_store_id = item.get('store_id')
        if raw_store_id is None:
            raw_store_id = item.get('storeId')
        store_id = str(raw_store_id).strip()
        if not store_id:
            continue
        latitude = item.get('latitude')
        longitude = item.get('longitude')
        if latitude is None or longitude is None:
            continue
        try:
            latitude_value = float(latitude)
            longitude_value = float(longitude)
        except (TypeError, ValueError):
            continue
        record = {
            "provider": "menards",
            "store_id": store_id,
            "name": str(item.get('name') or item.get('storeName') or f"Menards #{store_id}").strip(),
            "street": str(item.get('street') or item.get('address') or '').strip(),
            "city": str(item.get('city') or '').strip(),
            "state": str(item.get('state') or '').strip(),
            "zip": str(item.get('zip') or item.get('postalCode') or '').strip(),
            "latitude": latitude_value,
            "longitude": longitude_value,
            "open": bool(item.get('open', True)),
            "services": list(item.get('services') or []),
            "detail_url": f"https://www.menards.com/store-details/store.html?store={store_id}",
        }
        if not record["street"] and record["city"] and record["state"]:
            record["street"] = record["city"]
        records.append(record)
    return records


def _extract_address(page_text: str) -> Optional[str]:
    text = html_lib.unescape((page_text or "").replace("&nbsp;", " "))
    text = text.replace("\xa0", " ")
    if not text:
        return None

    candidates = [text]
    candidates.extend([
        re.sub(r"([A-Z]{2})(\d{5})\b", r"\1 \2", text, flags=re.I),
        re.sub(r"\s*([A-Z]{2})\s+(\d{5})\b", r" \1 \2", text, flags=re.I),
        re.sub(r"(?<=\d)\s*,\s*(?=[A-Z]{2}\s*\d{5})", ", ", text, flags=re.I),
    ])

    patterns = [
        r"\d+\s+[A-Za-z0-9.\- ]+,\s*[A-Z][A-Za-z.\- ]+,\s*[A-Z]{2}\s*\d{5}",
        r"\d+\s+[A-Za-z0-9.\- ]+,\s*[A-Z][A-Za-z.\- ]+,\s*[A-Z]{2}\s*\d{5}-\d{4}",
        r"\d+\s+[A-Za-z0-9.\- ]+,\s*[A-Z][A-Za-z.\- ]+\s+[A-Z]{2}\s*\d{5}",
        r"\d+\s+[A-Za-z0-9.\- ]+,\s*[A-Z][A-Za-z.\- ]+,\s*[A-Z]{2}\d{5}",
        r"\d+\s+[A-Za-z0-9.\- ]+,\s*[A-Za-z.\- ]+,\s*[A-Z]{2}\s*\d{5}",
    ]
    for candidate in candidates:
        for pattern in patterns:
            match = re.search(pattern, candidate, re.I)
            if match:
                clean = _normalize_address(match.group(0))
                if re.search(r"\d{5}$", clean, re.I):
                    return clean

    for line in text.splitlines():
        for pattern in patterns:
            match = re.search(pattern, line, re.I)
            if match:
                clean = _normalize_address(match.group(0))
                if re.search(r"\d{5}$", clean, re.I):
                    return clean
    return None


def geocode_address(address: str) -> Dict[str, float]:
    if not address:
        raise ValueError("Address is required for geocoding")
    url = "https://nominatim.openstreetmap.org/search?format=jsonv2&q=" + quote(address)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as result:
        payload = json.loads(result.read().decode("utf-8"))
    if not payload:
        raise ValueError(f"Address could not be geocoded: {address}")
    first = payload[0]
    return {"latitude": float(first["lat"]), "longitude": float(first["lon"]) }


def _load_cached_metadata(store_id: str) -> Optional[Dict[str, Any]]:
    path = _metadata_path(store_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return raw


def _save_metadata(store_id: str, data: Dict[str, Any]) -> None:
    path = _metadata_path(store_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _extract_svg_response(page) -> Optional[tuple[str, str]]:
    svg_url: Optional[str] = None
    svg_text: Optional[str] = None

    def handler(response):
        nonlocal svg_url, svg_text
        if svg_url is not None:
            return
        content_type = (response.headers or {}).get("content-type", "").lower()
        url = response.url or ""
        if "image/svg+xml" in content_type and ("storemap" in url.lower() or "/assets/storemap/" in url.lower() or "/storemap/" in url.lower()):
            try:
                body = response.body()
            except Exception:
                return
            if not body:
                return
            svg_url = url
            svg_text = body.decode("utf-8", errors="replace")

    page.on("response", handler)
    return (svg_url, svg_text) if svg_url and svg_text else None


def fetch_menards_store(store_url: str, force_refresh: bool = False) -> Dict[str, Any]:
    store_id = extract_store_id(store_url)
    cached = _load_cached_metadata(store_id) if not force_refresh else None
    if not force_refresh and cached and cached.get("svg_path"):
        svg_file = Path(cached["svg_path"])
        if svg_file.exists():
            return cached

    if force_refresh:
        svg_path = _svg_path(store_id)
        if svg_path.exists():
            svg_path.unlink()
        meta_path = _metadata_path(store_id)
        if meta_path.exists():
            meta_path.unlink()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        svg_url: Optional[str] = None
        svg_text: Optional[str] = None

        def capture_svg(response):
            nonlocal svg_url, svg_text
            if svg_url is not None:
                return
            content_type = (response.headers or {}).get("content-type", "").lower()
            url = response.url or ""
            lower_url = url.lower()
            matches_svg = "image/svg+xml" in content_type and ("storemap" in lower_url or "/assets/storemap/" in lower_url or "/storemap/" in lower_url)
            if not matches_svg:
                return
            try:
                body = response.body()
            except Exception:
                return
            if not body:
                return
            svg_url = url
            svg_text = body.decode("utf-8", errors="replace")

        page.on("response", capture_svg)
        page.goto(store_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(15000)

        page_text = page.locator("body").inner_text(timeout=20000)
        html_text = page.content()
        address = _extract_address(page_text or html_text) or _extract_address(html_text)
        if not address:
            body_text = page.evaluate("document.body ? document.body.innerText : ''")
            address = _extract_address(body_text) or _extract_address(html_text)
        if not address:
            for selector in ["text=Store Map", "text=Store Details", "text=Floor 1", "text=Floor Map"]:
                try:
                    button = page.locator(selector).first
                    if button.count() and button.is_visible():
                        button.click(timeout=20000)
                        page.wait_for_timeout(5000)
                        page_text = page.locator("body").inner_text(timeout=20000)
                        address = _extract_address(page_text)
                        if address:
                            break
                except Exception:
                    pass

        if not address:
            raise ValueError("Address could not be extracted from the Menards store page")

        if svg_url is None:
            maybe = [
                response for response in page.context.request.history() if response.url.lower().find("storemap") >= 0
            ]
            if maybe:
                svg_url = maybe[0].url

        if svg_url is None or svg_text is None:
            raise ValueError("SVG network request was not found for this Menards store")

        coordinates = geocode_address(address)
        svg_dir = _store_dir(store_id)
        svg_dir.mkdir(parents=True, exist_ok=True)

        if not svg_text.strip().startswith("<svg"):
            raise ValueError("Downloaded Menards SVG is invalid or not an SVG document")

        viewbox_match = re.search(r'viewBox=["\']([^"\']+)["\']', svg_text, re.I)
        if not viewbox_match:
            raise ValueError("Downloaded Menards SVG is missing a valid viewBox")

        svg_path = svg_dir / "floor1.svg"
        svg_path.write_text(svg_text, encoding="utf-8")
        metadata = {
            "store_id": store_id,
            "store_url": store_url,
            "name": f"Sioux City Menards",
            "address": address,
            "city": "Sioux City",
            "state": "IA",
            "state_code": "IA",
            "latitude": coordinates["latitude"],
            "longitude": coordinates["longitude"],
            "svg_url": svg_url,
            "svg_path": str(svg_path),
            "svg_size": svg_path.stat().st_size,
            "viewBox": viewbox_match.group(1),
            "status": "ready",
            "indoor_map": "ready",
            "location": "ready",
        }
        _save_metadata(store_id, metadata)
        browser.close()
        return metadata


def _locator_cache_path() -> Path:
    MENARDS_DIR.mkdir(parents=True, exist_ok=True)
    return MENARDS_DIR / "locator_stores.json"


def _stores_json_path() -> Path:
    data_dir = DATA_DIR / "menards"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "stores.json"


def _load_locator_cache() -> Optional[Dict[str, Any]]:
    path = _locator_cache_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(raw, dict):
        return raw
    return None


def _save_locator_cache(payload: Dict[str, Any]) -> None:
    path = _locator_cache_path()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _save_normalized_store_file(records: Iterable[Dict[str, Any]]) -> None:
    path = _stores_json_path()
    payload = [dict(item) for item in records]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_local_menards_store_file() -> List[Dict[str, Any]]:
    path = _stores_json_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict):
        raw = raw.get("stores")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _uploaded_menards_html_path() -> Optional[Path]:
    candidates = [
        Path.cwd() / "Menard_location.txt",
        Path.cwd() / "data" / "menards" / "Menard_location.txt",
        Path.cwd() / "data" / "Menard_location.txt",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def fetch_menards_locator_stores(force_refresh: bool = False) -> Dict[str, Any]:
    """Return the Menards locator dataset from the HTML payload on the locator page.

    This is the confirmed source: the page contains the initialStores meta tag and
    exposes the full store list with coordinates embedded in the HTML itself.
    We do not geocode these records because the coordinates are already present.
    """
    if not force_refresh:
        uploaded_html_path = _uploaded_menards_html_path()
        if uploaded_html_path is not None:
            uploaded_html = uploaded_html_path.read_text(encoding="utf-8", errors="replace")
            store_rows = parse_initial_store_records(uploaded_html)
            if len(store_rows) >= 100:
                _save_normalized_store_file(store_rows)
                states = sorted({record["state"] for record in store_rows if record.get("state")})
                cities = sorted({record["city"] for record in store_rows if record.get("city")})
                payload = {
                    "provider": "menards",
                    "stores": store_rows,
                    "count": len(store_rows),
                    "source": "uploaded_locator_html",
                    "status": "ready",
                    "message": "Menards locator data loaded from the uploaded locator HTML.",
                    "states": states,
                    "cities": cities,
                    "last_updated": None,
                }
                _save_locator_cache(payload)
                return payload
        cached = _load_locator_cache()
        if cached is not None:
            store_count = len(cached.get("stores") or []) if isinstance(cached, dict) else 0
            if store_count >= 100:
                return {**cached, "source": "cache"}
        file_records = _load_local_menards_store_file()
        if file_records and len(file_records) >= 100:
            states = sorted({record["state"] for record in file_records if record.get("state")})
            cities = sorted({record["city"] for record in file_records if record.get("city")})
            payload = {
                "provider": "menards",
                "stores": file_records,
                "count": len(file_records),
                "source": "cache",
                "status": "ready",
                "message": "Loaded Menards store records from the saved locator dataset.",
                "states": states,
                "cities": cities,
                "last_updated": None,
            }
            _save_locator_cache(payload)
            return payload

    locator_url = "https://www.menards.com/store-details/locator.html"
    logger.info("Fetching Menards locator...")
    try:
        request = Request(
            locator_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(request, timeout=30) as response:
            http_status = getattr(response, "status", 200)
            html = response.read().decode("utf-8", errors="replace")
        logger.info("HTTP status: %s", http_status)
    except Exception as exc:
        logger.warning("Fetching Menards locator failed: %s", exc)
        file_records = _load_local_menards_store_file()
        if file_records and len(file_records) >= 100:
            states = sorted({record["state"] for record in file_records if record.get("state")})
            cities = sorted({record["city"] for record in file_records if record.get("city")})
            payload = {
                "provider": "menards",
                "stores": file_records,
                "count": len(file_records),
                "source": "cache",
                "status": "ready",
                "message": "Menards locator fetch failed; using previous cache.",
                "states": states,
                "cities": cities,
                "last_updated": None,
            }
            _save_locator_cache(payload)
            return payload
        payload = {
            "provider": "menards",
            "stores": [],
            "count": 0,
            "source": "live",
            "status": "blocked",
            "message": "Menards locator blocked or incomplete",
            "states": [],
            "cities": [],
            "last_updated": None,
        }
        _save_locator_cache(payload)
        return payload

    lower_html = html.lower()
    has_initial_stores = 'id="initialstores"' in lower_html and 'data-initial-stores' in lower_html
    logger.info("initialStores found: %s", has_initial_stores)
    if not has_initial_stores:
        file_records = _load_local_menards_store_file()
        if file_records and len(file_records) >= 100:
            states = sorted({record["state"] for record in file_records if record.get("state")})
            cities = sorted({record["city"] for record in file_records if record.get("city")})
            payload = {
                "provider": "menards",
                "stores": file_records,
                "count": len(file_records),
                "source": "cache",
                "status": "ready",
                "message": "Menards locator blocked or incomplete; using previous cache.",
                "states": states,
                "cities": cities,
                "last_updated": None,
            }
            _save_locator_cache(payload)
            return payload
        payload = {
            "provider": "menards",
            "stores": [],
            "count": 0,
            "source": "live",
            "status": "blocked",
            "message": "Menards locator blocked or incomplete",
            "states": [],
            "cities": [],
            "last_updated": None,
        }
        _save_locator_cache(payload)
        return payload

    store_rows = parse_initial_store_records(html)
    logger.info("raw store records: %s", len(store_rows))
    valid_coordinate_records = sum(1 for row in store_rows if row.get("latitude") is not None and row.get("longitude") is not None)
    logger.info("valid coordinate records: %s", valid_coordinate_records)
    if len(store_rows) < 100:
        file_records = _load_local_menards_store_file()
        if file_records and len(file_records) >= 100:
            states = sorted({record["state"] for record in file_records if record.get("state")})
            cities = sorted({record["city"] for record in file_records if record.get("city")})
            payload = {
                "provider": "menards",
                "stores": file_records,
                "count": len(file_records),
                "source": "cache",
                "status": "ready",
                "message": "Menards locator parsing incomplete; using previous cache.",
                "states": states,
                "cities": cities,
                "last_updated": None,
            }
            _save_locator_cache(payload)
            return payload
        raise ValueError("Menards locator parsing incomplete")
    _save_normalized_store_file(store_rows)
    states = sorted({record["state"] for record in store_rows if record.get("state")})
    cities = sorted({record["city"] for record in store_rows if record.get("city")})
    payload = {
        "provider": "menards",
        "stores": store_rows,
        "count": len(store_rows),
        "source": "live",
        "status": "ready",
        "message": "Menards locator data loaded from the initialStores HTML payload.",
        "states": states,
        "cities": cities,
        "last_updated": None,
    }
    _save_locator_cache(payload)
    return payload


def get_cached_menards_store(store_id: str) -> Optional[Dict[str, Any]]:
    return _load_cached_metadata(store_id)
