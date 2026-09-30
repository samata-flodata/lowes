import json

from backend import menards_service
from backend.menards_service import fetch_menards_locator_stores, parse_initial_store_records


def test_parse_initial_stores_payload_from_locator_html():
    html = '''
    <html><head>
      <meta id="initialStores" data-initial-stores="[{&quot;number&quot;:3132,&quot;name&quot;:&quot;ABERDEEN&quot;,&quot;street&quot;:&quot;3821 6TH AVE SE&quot;,&quot;city&quot;:&quot;ABERDEEN&quot;,&quot;state&quot;:&quot;SD&quot;,&quot;zip&quot;:&quot;57401&quot;,&quot;latitude&quot;:&quot;45.460465&quot;,&quot;longitude&quot;:&quot;-98.435885&quot;},{&quot;number&quot;:3163,&quot;name&quot;:&quot;BAXTER&quot;,&quot;street&quot;:&quot;15236 DELLWOOD DR&quot;,&quot;city&quot;:&quot;BAXTER&quot;,&quot;state&quot;:&quot;MN&quot;,&quot;zip&quot;:&quot;56401&quot;,&quot;latitude&quot;:&quot;46.372145&quot;,&quot;longitude&quot;:&quot;-94.241481&quot;}]" />
    </head></html>
    '''

    records = parse_initial_store_records(html)

    assert len(records) == 2
    record = records[1]
    assert record["provider"] == "menards"
    assert record["store_id"] == "3163"
    assert record["name"] == "BAXTER"
    assert record["street"] == "15236 DELLWOOD DR"
    assert record["city"] == "BAXTER"
    assert record["state"] == "MN"
    assert record["zip"] == "56401"
    assert record["latitude"] == 46.372145
    assert record["longitude"] == -94.241481
    assert record["detail_url"] == "https://www.menards.com/store-details/store.html?store=3163"


def test_parse_initial_stores_payload_supports_full_locator_array():
    records = [{
        "number": i,
        "name": f"STORE {i}",
        "street": f"{i} MAIN ST",
        "city": "CITY",
        "state": "MN",
        "zip": "55555",
        "latitude": str(44.0 + i / 1000),
        "longitude": str(-95.0 - i / 1000),
    } for i in range(120)]

    html = '<meta id="initialStores" data-initial-stores="' + __import__("json").dumps(records).replace('"', '&quot;') + '" />'
    parsed = parse_initial_store_records(html)

    assert len(parsed) == 120
    assert parsed[0]["store_id"] == "0"
    assert parsed[-1]["store_id"] == "119"
    assert len({item["state"] for item in parsed}) == 1


def test_fetch_menards_locator_stores_ignores_stale_singleton_cache(monkeypatch, tmp_path):
    stale_payload = {
        "provider": "menards",
        "stores": [{
            "provider": "menards",
            "store_id": "3163",
            "name": "BAXTER MENARDS",
            "street": "15236 DELLWOOD DR",
            "city": "BAXTER",
            "state": "MN",
            "zip": "56401",
            "latitude": 46.372145,
            "longitude": -94.241481,
            "detail_url": "https://www.menards.com/store-details/store.html?store=3163",
        }],
        "count": 1,
        "source": "cache",
        "status": "ready",
    }
    live_records = [{
        "number": index,
        "name": f"STORE {index}",
        "street": f"{index} MAIN ST",
        "city": "CITY",
        "state": "MN",
        "zip": "55555",
        "latitude": str(44.0 + index / 1000),
        "longitude": str(-95.0 - index / 1000),
    } for index in range(120)]
    html = '<meta id="initialStores" data-initial-stores="' + json.dumps(live_records).replace('"', '&quot;') + '" />'

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return html.encode("utf-8")

    monkeypatch.setattr(menards_service, "_locator_cache_path", lambda: tmp_path / "locator_stores.json")
    monkeypatch.setattr(menards_service, "_stores_json_path", lambda: tmp_path / "stores.json")
    monkeypatch.setattr(menards_service, "_load_locator_cache", lambda: stale_payload)
    monkeypatch.setattr(menards_service, "urlopen", lambda *args, **kwargs: FakeResponse())

    payload = fetch_menards_locator_stores(force_refresh=False)

    assert payload["count"] == 120
    assert payload["stores"][0]["store_id"] == "0"
    assert payload["stores"][-1]["store_id"] == "119"


def test_fetch_all_menards_indoor_maps_skips_cached_and_marks_unavailable(tmp_path, monkeypatch):
    data_dir = tmp_path / "menards"
    data_dir.mkdir(parents=True, exist_ok=True)
    cached_svg = data_dir / "1001" / "floor1.svg"
    cached_svg.parent.mkdir(parents=True, exist_ok=True)
    cached_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>', encoding="utf-8")
    (data_dir / "1001" / "metadata.json").write_text(json.dumps({
        "store_id": "1001",
        "status": "available",
        "svg_url": "https://example.test/1001.svg",
        "svg_path": str(cached_svg),
    }), encoding="utf-8")

    monkeypatch.setattr(menards_service, "MENARDS_DIR", data_dir)

    def fake_fetch_locator(force_refresh=False):
        return {
            "provider": "menards",
            "stores": [
                {"store_id": "1001"},
                {"store_id": "1002"},
                {"store_id": "1003"},
            ],
            "count": 3,
            "status": "ready",
        }

    def fake_discover(store_id, force_refresh=False):
        if store_id == "1002":
            svg_path = data_dir / store_id / "floor1.svg"
            svg_path.parent.mkdir(parents=True, exist_ok=True)
            svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>', encoding="utf-8")
            return {
                "store_id": str(store_id),
                "status": "available",
                "svg_url": "https://example.test/1002.svg",
                "svg_path": str(svg_path),
                "local_file": str(svg_path),
            }
        raise ValueError(f"No SVG request was discovered for Menards store {store_id}")

    monkeypatch.setattr(menards_service, "fetch_menards_locator_stores", fake_fetch_locator)
    monkeypatch.setattr(menards_service, "discover_menards_svg_for_store", fake_discover)

    summary = menards_service.fetch_all_menards_indoor_maps(store_ids=["1001", "1002", "1003"], force_refresh=False, concurrency=1, delay=0)

    assert summary["total_stores"] == 3
    assert summary["skipped_existing"] == 1
    assert summary["available"] == 1
    assert summary["unavailable"] == 1
    assert summary["failed"] == 0
    assert summary["failed_store_ids"] == []
    assert summary["unavailable_store_ids"] == ["1003"]
