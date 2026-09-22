from fastapi.testclient import TestClient

from backend.main import app
from backend.menards_service import _extract_address


def test_menards_fetch_route_uses_store_url(monkeypatch):
    expected = {
        "store_id": "3065",
        "store_url": "https://www.menards.com/store-details/store.html?store=3065",
        "address": "5900 Gordon Dr, Sioux City, IA 51106",
        "latitude": 42.4525,
        "longitude": -96.3816,
        "svg_path": "/tmp/floor1.svg",
    }

    def fake_fetch(store_url, force_refresh=False):
        assert store_url == expected["store_url"]
        return expected

    monkeypatch.setattr("backend.main.fetch_menards_store", fake_fetch)

    client = TestClient(app)
    response = client.post("/api/menards/fetch", json={"store_url": expected["store_url"]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["store_id"] == "3065"
    assert payload["address"] == expected["address"]


def test_extract_address_handles_state_zip_without_space():
    page_text = "SIOUX CITY\n5900 GORDON DR, SIOUX CITY, IA51106\nSIOUX CITY Phone Number"
    assert _extract_address(page_text) == "5900 GORDON DR, SIOUX CITY, IA 51106"
