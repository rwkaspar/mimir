import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

playwright_api = pytest.importorskip('playwright.sync_api')

SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shots')


@pytest.fixture(scope='session')
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser installed here
            pytest.skip('no Chromium for Playwright: %s' % str(e).splitlines()[0])
        yield b, p.devices['Pixel 7']
        b.close()


class Phone:
    """One phone browser tab, recording everything that goes wrong in it."""

    def __init__(self, browser, device, locale):
        self.context = browser.new_context(**device, locale=locale)
        self.page = self.context.new_page()
        self.problems = []
        self.page.on('pageerror', lambda e: self.problems.append('script error: %s' % e))
        self.page.on('console', lambda m: m.type == 'error' and self.problems.append(
            'console error: %s' % m.text))
        self.page.on('response', lambda r: r.status >= 400 and self.problems.append(
            'HTTP %d %s' % (r.status, r.url.split('?')[0])))
        self.page.on('requestfailed', lambda r: self.problems.append(
            'request failed: %s' % r.url.split('?')[0]))

    def open(self, url):
        self.page.goto(url, wait_until='networkidle')
        return self.page

    def shot(self, name):
        os.makedirs(SHOTS, exist_ok=True)
        self.page.screenshot(path=os.path.join(SHOTS, name + '.png'), full_page=True)

    def close(self):
        self.context.close()


@pytest.fixture
def phone(browser):
    b, device = browser
    made = []

    def make(locale='de-DE'):
        ph = Phone(b, device, locale)
        made.append(ph)
        return ph
    yield make
    for ph in made:
        ph.close()
