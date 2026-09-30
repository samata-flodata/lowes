from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
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


def _store_detail_url(store_id: str) -> str:
    return f"https://www.menards.com/store-details/store.html?store={store_id}"


def _discover_svg_urls_from_html(html_text: str, page_url: str) -> List[str]:
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    seen: set[str] = set()
    for tag in soup.find_all(True):
        for attr in ("src", "data", "href"):
            value = tag.get(attr)
            if not value:
                continue
            candidate = str(value).strip()
            if ".svg" not in candidate.lower():
                continue
            if "storemap" not in candidate.lower() and "/assets/storemap/" not in candidate.lower() and "/storemap/" not in candidate.lower():
                continue
            url = candidate if candidate.startswith(("http://", "https://")) else f"{page_url.rsplit('/', 1)[0]}/{candidate.lstrip('/')}"
            if url not in seen:
                seen.add(url)
    for pattern in [
        r'https?://[^\s"\']+storemap[^\s"\']*\.svg(?:\?[^\s"\']*)?',
        r'https?://[^\s"\']+/assets/storemap/[^\s"\']*\.svg(?:\?[^\s"\']*)?',
    ]:
        for match in re.findall(pattern, html_text, re.I):
            if match and match not in seen:
                seen.add(match)
    return sorted(seen)


def _validate_svg_response(svg_bytes: bytes, svg_url: str) -> str:
    if not svg_bytes:
        raise ValueError(f"SVG download failed for {svg_url}")
    text = svg_bytes.decode("utf-8", errors="replace")
    if "<svg" not in text.lower():
        raise ValueError(f"Downloaded file is not a valid SVG: {svg_url}")
    viewbox_match = re.search(r'viewBox=["\']([^"\']+)["\']', text, re.I)
    if not viewbox_match:
        raise ValueError(f"SVG viewBox missing from {svg_url}")
    return text


def _download_svg_text(svg_url: str) -> str:
    request = Request(
        svg_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "image/svg+xml,image/*;q=0.8,*/*;q=0.5",
        },
    )
    with urlopen(request, timeout=45) as response:
        content_type = (response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else (response.headers.get("Content-Type") or "")).lower()
        payload = response.read()
    if "image/svg+xml" not in content_type and "svg" not in content_type:
        raise ValueError(f"SVG response content type invalid for {svg_url}: {content_type}")
    return _validate_svg_response(payload, svg_url)


def discover_menards_svg_for_store(store_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    store_id = str(store_id)
    store_url = _store_detail_url(store_id)
    cached = _load_cached_metadata(store_id) if not force_refresh else None
    if cached and cached.get("svg_path"):
        svg_path = Path(cached["svg_path"])
        if svg_path.exists() and svg_path.is_file():
            return cached
    if force_refresh:
        svg_path = _svg_path(store_id)
        if svg_path.exists():
            svg_path.unlink()
        metadata_path = _metadata_path(store_id)
        if metadata_path.exists():
            metadata_path.unlink()

    try:
        request = Request(
            store_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(request, timeout=45) as response:
            html_text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(f"Store page unavailable for {store_id}: {exc}") from exc

    candidates = _discover_svg_urls_from_html(html_text, store_url)
    svg_url = None
    if candidates:
        svg_url = candidates[0]

    if svg_url is None:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            svg_url = None
            captured = None
            def capture(response):
                nonlocal svg_url, captured
                if svg_url is not None:
                    return
                content_type = (response.headers or {}).get("content-type", "").lower()
                url = response.url or ""
                lower_url = url.lower()
                if "image/svg+xml" in content_type and ("storemap" in lower_url or "/assets/storemap/" in lower_url or "/storemap/" in lower_url):
                    try:
                        payload = response.body()
                    except Exception:
                        return
                    if payload:
                        svg_url = url
                        captured = payload.decode("utf-8", errors="replace")
            page.on("response", capture)
            page.goto(store_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(12000)
            if svg_url is None and captured is None:
                browser.close()
                raise ValueError(f"No SVG request was discovered for Menards store {store_id}")
            browser.close()

    svg_text = _download_svg_text(svg_url)
    svg_dir = _store_dir(store_id)
    svg_dir.mkdir(parents=True, exist_ok=True)
    svg_path = svg_dir / "floor1.svg"
    svg_path.write_text(svg_text, encoding="utf-8")

    viewbox_match = re.search(r'viewBox=["\']([^"\']+)["\']', svg_text, re.I)
    if not viewbox_match:
        raise ValueError(f"SVG is missing a valid viewBox for {store_id}")

    metadata = {
        "store_id": store_id,
        "store_url": store_url,
        "svg_url": svg_url,
        "svg_path": str(svg_path),
        "local_svg": str(svg_path),
        "svg_size": svg_path.stat().st_size,
        "viewBox": viewbox_match.group(1),
        "status": "ready",
        "indoor_map": "ready",
        "location": "ready",
    }
    _save_metadata(store_id, metadata)
    return metadata


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


def _is_valid_svg_file(svg_path: Path) -> bool:
    if svg_path is None or not svg_path.exists() or not svg_path.is_file():
        return False
    try:
        size = svg_path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    try:
        text = svg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    lowered = text.lower()
    if "<svg" not in lowered:
        return False
    if "<html" in lowered and "<svg" not in lowered:
        return False
    return True


def _mark_store_status(
    store_id: str,
    status: str,
    *,
    svg_url: Optional[str] = None,
    local_file: Optional[str] = None,
    message: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metadata = _load_cached_metadata(store_id) or {"store_id": str(store_id)}
    payload = dict(metadata)
    payload.update({
        "store_id": str(store_id),
        "status": status,
        "svg_url": svg_url or payload.get("svg_url"),
        "local_file": local_file or payload.get("local_file"),
        "message": message or payload.get("message"),
    })
    if extra:
        payload.update(extra)
    _save_metadata(store_id, payload)
    return payload


def fetch_all_menards_indoor_maps(
    store_ids: Optional[List[str]] = None,
    force_refresh: bool = False,
    concurrency: int = 3,
    delay: float = 0.25,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Fetch and cache indoor SVGs for every Menards store in the dataset.

    The process is intentionally resumable: valid cached SVGs are skipped, while
    missing or invalid files are fetched and written per-store under
    data/menards/<store_id>/floor1.svg.
    """
    locator = fetch_menards_locator_stores(force_refresh=False)
    rows = locator.get("stores") if isinstance(locator, dict) else []
    if not isinstance(rows, list) or not rows:
        rows = _load_local_menards_store_file()

    all_store_ids: List[str] = []
    if store_ids:
        all_store_ids = [str(item).strip() for item in store_ids if str(item).strip()]
    else:
        for record in rows:
            if not isinstance(record, dict):
                continue
            item_id = str(record.get("store_id") or record.get("storeNumber") or record.get("number") or "").strip()
            if item_id:
                all_store_ids.append(item_id)

    unique_store_ids: List[str] = []
    seen: set[str] = set()
    for store_id in all_store_ids:
        if store_id not in seen:
            seen.add(store_id)
            unique_store_ids.append(store_id)

    summary = {
        "total_stores": len(unique_store_ids),
        "processed": 0,
        "available": 0,
        "unavailable": 0,
        "failed": 0,
        "blocked": 0,
        "skipped_existing": 0,
        "failed_store_ids": [],
        "unavailable_store_ids": [],
        "blocked_store_ids": [],
        "available_store_ids": [],
        "current": None,
        "status": "running",
    }

    for idx, store_id in enumerate(unique_store_ids, start=1):
        summary["current"] = {"store_id": store_id, "processed": idx, "total": len(unique_store_ids)}
        if progress_callback:
            progress_callback(dict(summary))

        svg_path = _svg_path(store_id)
        if not force_refresh and _is_valid_svg_file(svg_path):
            summary["skipped_existing"] += 1
            summary["processed"] += 1
            current_metadata = _load_cached_metadata(store_id) or {"store_id": store_id, "status": "available", "local_file": str(svg_path)}
            current_metadata.setdefault("status", "available")
            current_metadata["local_file"] = str(svg_path)
            current_metadata["svg_path"] = str(svg_path)
            current_metadata["status"] = "available"
            _save_metadata(store_id, current_metadata)
            if progress_callback:
                progress_callback(dict(summary))
            if delay > 0:
                time.sleep(delay)
            continue

        try:
            metadata = discover_menards_svg_for_store(store_id, force_refresh=force_refresh)
            svg_file = Path(metadata.get("svg_path") or metadata.get("local_file") or _svg_path(store_id))
            if metadata and svg_file.exists() and _is_valid_svg_file(svg_file):
                summary["available"] += 1
                summary["available_store_ids"].append(store_id)
                summary["processed"] += 1
                metadata["status"] = "available"
                metadata["local_file"] = str(svg_file)
                metadata["svg_path"] = str(svg_file)
                _save_metadata(store_id, metadata)
                summary["current"] = {"store_id": store_id, "processed": idx, "total": len(unique_store_ids), "status": "available"}
            else:
                raise ValueError(f"No SVG request was discovered for Menards store {store_id}")
        except ValueError as exc:
            lower = str(exc).lower()
            if "blocked" in lower or "challenge" in lower or "captcha" in lower or "403" in lower:
                status = "blocked"
                summary["blocked"] += 1
                summary["blocked_store_ids"].append(store_id)
            else:
                status = "unavailable"
                summary["unavailable"] += 1
                summary["unavailable_store_ids"].append(store_id)
            summary["processed"] += 1
            _mark_store_status(
                store_id,
                status,
                svg_url=None,
                local_file=None,
                message=str(exc),
                extra={"error": str(exc)},
            )
            summary["current"] = {"store_id": store_id, "processed": idx, "total": len(unique_store_ids), "status": status}
        except Exception as exc:
            summary["failed"] += 1
            summary["failed_store_ids"].append(store_id)
            summary["processed"] += 1
            _mark_store_status(
                store_id,
                "failed",
                svg_url=None,
                local_file=None,
                message=str(exc),
                extra={"error": str(exc)},
            )
            summary["current"] = {"store_id": store_id, "processed": idx, "total": len(unique_store_ids), "status": "failed"}

        if progress_callback:
            progress_callback(dict(summary))
        if delay > 0 and idx < len(unique_store_ids):
            time.sleep(delay)

    summary["status"] = "complete"
    summary["total_stores"] = len(unique_store_ids)
    summary["processed_total"] = summary["processed"]
    if progress_callback:
        progress_callback(dict(summary))
    return summary


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
    metadata = discover_menards_svg_for_store(store_id, force_refresh=force_refresh)
    try:
        locator = fetch_menards_locator_stores(force_refresh=False)
        for record in locator.get("stores", []):
            if str(record.get("store_id")) == str(store_id):
                metadata["name"] = record.get("name") or metadata.get("name") or f"Menards #{store_id}"
                metadata["address"] = (record.get("street") or "") + ", " + (record.get("city") or "") + ", " + (record.get("state") or "") + " " + (record.get("zip") or "")
                metadata["city"] = record.get("city") or metadata.get("city")
                metadata["state"] = record.get("state") or metadata.get("state")
                metadata["state_code"] = record.get("state") or metadata.get("state_code")
                metadata["latitude"] = record.get("latitude")
                metadata["longitude"] = record.get("longitude")
                break
    except Exception:
        pass
    if "address" not in metadata:
        metadata["address"] = store_url
    _save_metadata(store_id, metadata)
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
