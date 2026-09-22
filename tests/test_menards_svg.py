from pathlib import Path


def test_menards_svg_background_removed():
    svg_path = Path(__file__).resolve().parents[1] / "frontend" / "R5EMMR(BAXT)-239.svg"

    assert svg_path.exists()
    text = svg_path.read_text(encoding="utf-8")
    assert 'viewBox="0 0 2277.64 1728"' in text
    assert 'M0 0h2277.64v1728H0z' not in text
