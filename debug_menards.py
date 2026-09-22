from playwright.sync_api import sync_playwright
from backend.menards_service import _extract_address

url = 'https://www.menards.com/store-details/store.html?store=3065'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1200})
    page.goto(url, wait_until='domcontentloaded', timeout=120000)
    page.wait_for_timeout(15000)
    body_text = page.locator('body').inner_text(timeout=20000)
    html_text = page.content()
    print('BODY_LEN', len(body_text))
    print('HTML_LEN', len(html_text))
    print('BODY_SNIP', repr(body_text[:1000]))
    idx = html_text.lower().find('5900')
    print('IDX', idx)
    print('HTML_SNIP', repr(html_text[idx-200:idx+500] if idx != -1 else html_text[:800]))
    print('EXTRACT_BODY', _extract_address(body_text))
    print('EXTRACT_HTML', _extract_address(html_text))
    browser.close()
