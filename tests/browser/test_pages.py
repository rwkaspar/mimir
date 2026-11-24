"""The phone pages in a real mobile browser against the real server.

Nobody had opened these pages in a browser before they shipped; the first
phone found that every thumbnail was a 404. Each test here loads a page the
way a phone does (access token, touch, small screen), fails on any script
error, failed request or missing picture, and saves a screenshot to
tests/browser/shots/ so a human can look at what was built.
"""
import json
import re
import time

import pytest

from harness import start_frame
from test_guards import PAGES, top_level_names

RAW_KEY = re.compile(r'^[a-z]+(_[a-z0-9]+)+$')      # looks like an untranslated key


@pytest.fixture
def frame(tmp_path):
    f = start_frame(tmp_path)
    yield f
    f.stop()


def visible_text_problems(page):
    """Texts that are clearly not meant for a person."""
    return page.evaluate('''() => {
        const bad = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const n = walker.currentNode, el = n.parentElement;
            if (!el || ['SCRIPT', 'STYLE'].includes(el.tagName)) continue;
            if (!el.getClientRects().length) continue;            // not shown
            const s = n.textContent.trim();
            if (/\\bundefined\\b|\\bNaN\\b|\\[object |^null$/.test(s)) bad.push(s);
            if (/^[a-z]+(_[a-z0-9]+)+$/.test(s)) bad.push('raw key: ' + s);
        }
        return bad;
    }''')


def overflow(page):
    """Horizontal overflow: the page is wider than the phone."""
    return page.evaluate(
        '() => document.documentElement.scrollWidth - document.documentElement.clientWidth')


def check_clean(ph, name):
    ph.shot(name)
    problems = list(ph.problems) + visible_text_problems(ph.page)
    wide = overflow(ph.page)
    if wide > 2:
        problems.append('page is %dpx wider than the screen' % wide)
    assert not problems, '%s:\n  %s' % (name, '\n  '.join(problems))


# --- every page, both languages ------------------------------------------

@pytest.mark.parametrize('locale', ['de-DE', 'en-US'])
def test_settings_page(frame, phone, locale):
    ph = phone(locale)
    page = ph.open(frame.url('/remote'))
    assert page.locator('.brand svg.logo').is_visible()
    assert page.locator('#r-title').inner_text() == 'mimir'
    label = 'Bilder auswählen' if locale == 'de-DE' else 'Choose pictures'
    assert page.get_by_role('button', name=label).is_visible()
    # "Sync now" belongs to the pictures, "Save albums" to the album lists.
    order = page.evaluate('''() => {
        const y = id => document.getElementById(id).getBoundingClientRect().top;
        return [y('sync-btn'), y('r-syn-header'), y('r-save'), y('r-ha-header')];
    }''')
    assert order == sorted(order), 'buttons out of place: %s' % order
    check_clean(ph, 'settings-' + locale[:2])


def test_settings_page_eink(tmp_path, phone):
    f = start_frame(tmp_path, variant='eink', videos=0)
    try:
        ph = phone()
        page = ph.open(f.url('/remote'))
        hidden = page.evaluate(
            "() => [...document.querySelectorAll('.lcd-only')]"
            ".filter(e => e.getClientRects().length).map(e => e.textContent.trim().slice(0, 30))")
        assert not hidden, 'LCD-only settings shown on an e-ink frame: %s' % hidden
        check_clean(ph, 'settings-eink')
    finally:
        f.stop()


def test_wizard_page(tmp_path, phone):
    f = start_frame(tmp_path, setup_complete=False)
    try:
        ph = phone()
        ph.open(f.url('/'))
        check_clean(ph, 'wizard')
    finally:
        f.stop()


def wizard_after_language(tmp_path, phone, **cfg):
    f = start_frame(tmp_path, setup_complete=False,
                    extra=dict({'display_confirmed': False}, **cfg))
    ph = phone()
    ph.open(f.url('/'))
    ph.page.evaluate("setLanguage('en')")
    ph.page.wait_for_selector('.step.active:not(#step-1)')
    return f, ph


def shown(ph, selector):
    return ph.page.is_visible(selector)


def test_wizard_asks_for_the_display_on_a_source_install(tmp_path, phone):
    f, ph = wizard_after_language(tmp_path, phone)
    try:
        assert shown(ph, '#step-display.active')
        assert shown(ph, '#display-card-lcd') and shown(ph, '#display-card-eink')
        check_clean(ph, 'wizard-display')
    finally:
        f.stop()


def test_wizard_skips_the_display_on_the_hdmi_image(tmp_path, phone):
    f, ph = wizard_after_language(tmp_path, phone, variant_fixed=True)
    try:
        assert shown(ph, '#step-2.active') and not shown(ph, '#step-display')
    finally:
        f.stop()


def test_wizard_asks_only_for_the_battery_board_on_an_eink_image(tmp_path, phone):
    f, ph = wizard_after_language(tmp_path, phone, variant='eink', variant_fixed=True,
                                  eink={'driver': 'waveshare:epd7in3e'})
    try:
        assert shown(ph, '#step-display.active')
        assert not shown(ph, '#display-card-lcd') and not shown(ph, '#display-card-eink')
        assert not shown(ph, '#display-panel') and shown(ph, '#display-pisugar')
        assert 'battery board' in ph.page.inner_text('#display-title')
        check_clean(ph, 'wizard-eink-battery')
        ph.page.click('#display-next')                      # "none": straight on
        ph.page.wait_for_selector('#step-2.active')
        assert f.config()['eink'] == {'driver': 'waveshare:epd7in3e'}
    finally:
        f.stop()


def test_wifi_setup_page(tmp_path, phone):
    """As a phone meets it: on the frame's own setup hotspot, without a token."""
    f = start_frame(tmp_path, setup_complete=False, wifi_mode='hotspot')
    try:
        ph = phone()
        ph.open(f.url('/', token=False))
        assert 'TestNet' in ph.page.inner_text('body')        # the scanned network
        check_clean(ph, 'wifi-setup')
    finally:
        f.stop()


def test_without_the_token_a_phone_sees_only_the_notice(frame, phone):
    ph = phone()
    page = ph.open(frame.url('/remote', token=False))
    assert 'Access restricted' in page.inner_text('body')
    assert 'HTTP 401 %s/remote' % frame.base in ph.problems
    assert all('401' in p for p in ph.problems), ph.problems


# --- the pictures page ----------------------------------------------------

def thumbs(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.cell img')]"
        ".map(i => ({src: i.src.split('?')[0], ok: i.complete && i.naturalWidth > 0}))")


def wait_thumbs(page):
    page.wait_for_function(
        "() => { const i = [...document.querySelectorAll('.cell img')];"
        " return i.length && i.every(x => x.complete); }", timeout=30000)


@pytest.mark.parametrize('orientation', ['horizontal', 'vertical'])
def test_pictures_page_shows_every_picture(tmp_path, phone, orientation):
    f = start_frame(tmp_path, orientation=orientation)
    try:
        ph = phone()
        page = ph.open(f.url('/library'))
        page.evaluate('() => window.scrollTo(0, document.body.scrollHeight)')   # lazy images
        wait_thumbs(page)
        got = thumbs(page)
        want = json.loads(__import__('urllib.request').request.urlopen(
            f.url('/api/library')).read())['total']
        assert len(got) == want >= 5
        broken = [t['src'] for t in got if not t['ok']]
        assert not broken, 'thumbnails that did not load: %s' % broken
        page.evaluate('() => window.scrollTo(0, 0)')
        check_clean(ph, 'pictures-' + orientation)
    finally:
        f.stop()


def test_ticking_a_picture_off_and_on(frame, phone):
    ph = phone()
    page = ph.open(frame.url('/library'))
    wait_thumbs(page)
    total = page.locator('.cell').count()
    first = page.locator('.cell').first
    name = first.get_attribute('aria-label')
    assert first.get_attribute('aria-checked') == 'true'
    assert '%d von %d' % (total, total) in page.locator('#count').inner_text()

    first.tap()
    page.wait_for_function("() => document.querySelector('.cell').classList.contains('off')")
    deadline = time.time() + 5
    hidden_file = frame.photos_dir / 'hidden.json'
    while time.time() < deadline and not hidden_file.exists():
        time.sleep(0.05)
    assert json.loads(hidden_file.read_text()) == {'hidden': [name]}
    assert '%d von %d' % (total - 1, total) in page.locator('#count').inner_text()

    page.reload(wait_until='networkidle')                       # it was saved, not just shown
    assert page.locator('.cell').first.get_attribute('aria-checked') == 'false'
    page.get_by_role('button', name='Herausgenommen').tap()
    assert page.locator('.cell').count() == 1
    ph.shot('pictures-one-off')

    page.get_by_role('button', name='Alle anhaken').tap()
    page.wait_for_function("() => !document.querySelector('.cell.off')")
    assert json.loads(hidden_file.read_text()) == {'hidden': []}
    assert not ph.problems, ph.problems


def test_from_settings_to_pictures_and_back(frame, phone):
    """The link carries the token; arriving without it showed 'Access restricted'."""
    ph = phone()
    page = ph.open(frame.url('/remote'))
    page.get_by_role('button', name='Bilder auswählen').tap()
    page.wait_for_url('**/library**')
    wait_thumbs(page)
    assert all(t['ok'] for t in thumbs(page))
    page.locator('#back').tap()
    page.wait_for_url('**/remote**')
    assert page.locator('#r-title').is_visible()
    assert not ph.problems, ph.problems


# --- settings actually reach the config -----------------------------------

def test_a_setting_changed_on_the_phone_lands_in_the_config(frame, phone):
    ph = phone()
    page = ph.open(frame.url('/remote'))
    def interval():        # the server rewrites the file in place: may be mid-write
        return (frame.config().get('slideshow') or {}).get('interval')
    assert interval() == 20
    page.select_option('#set-interval', '10')
    deadline = time.time() + 5
    while time.time() < deadline and interval() != 10:
        time.sleep(0.05)
    assert interval() == 10
    assert not ph.problems, ph.problems


# --- names the browser owns ----------------------------------------------

def test_page_scripts_do_not_reuse_names_of_this_browser(browser):
    """`var orientation` in a page is window.orientation on a phone: read-only,
    always 0. Ask the real browser which names it owns."""
    b, device = browser
    ctx = b.new_context(**device)
    page = ctx.new_page()
    page.goto('about:blank')
    owned = set(page.evaluate('''() => {
        const names = new Set();
        for (let o = window; o; o = Object.getPrototypeOf(o))
            Object.getOwnPropertyNames(o).forEach(n => names.add(n));
        return [...names];
    }'''))
    ctx.close()
    assert 'location' in owned and len(owned) > 500
    clashes = {p.name: sorted(set(top_level_names(p)) & owned) for p in PAGES}
    clashes = {k: v for k, v in clashes.items() if v}
    assert not clashes, 'page scripts declare names the browser owns: %s' % clashes
