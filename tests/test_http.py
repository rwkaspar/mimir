"""The real web server over HTTP, seen the way a phone on the LAN sees it.

Why this exists: checks made from the frame itself (localhost) skip the
access token, and code that was only ever called as a function was never
routed. These tests go through the socket.
"""
import json
import time
import urllib.error
import urllib.request

import pytest

from harness import start_frame
from test_guards import server_routes


def call(frame, path, data=None, token=True, headers=None):
    req = urllib.request.Request(
        frame.url(path, token=token),
        data=None if data is None else json.dumps(data).encode(),
        headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def frame(tmp_path):
    f = start_frame(tmp_path)
    yield f
    f.stop()


def get_paths():
    exact, prefixes = server_routes()['GET']
    paths = sorted(exact - {'/favicon.ico', '/'}) + ['/']
    return paths


def test_a_phone_without_the_token_gets_nothing(frame):
    open_paths = []
    for path in get_paths() + ['/photos/horizontal/gph_00_gphoto_00.jpg',
                               '/api/library/thumb/horizontal/gph_00_gphoto_00.jpg']:
        status, _ = call(frame, path, token=False)
        if status != 401:
            open_paths.append('%s -> %s' % (path, status))
    assert not open_paths, 'readable without the access token:\n  ' + '\n  '.join(open_paths)


def test_a_phone_without_the_token_cannot_change_anything(frame):
    before = frame.config()
    exact, _ = server_routes()['POST']
    open_paths = []
    for path in sorted(exact):
        status, _ = call(frame, path, data={}, token=False)
        if status != 401:
            open_paths.append('%s -> %s' % (path, status))
    assert not open_paths, 'writable without the access token:\n  ' + '\n  '.join(open_paths)
    assert frame.config() == before


def test_the_token_works_as_query_header_and_cookie(frame):
    path = '/api/library/thumb/horizontal/gph_00_gphoto_00.jpg'
    assert call(frame, path)[0] == 200
    assert call(frame, path, token=False, headers={'X-Frame-Token': frame.token})[0] == 200
    assert call(frame, path, token=False,
                headers={'Cookie': 'a=b; pf_token=%s; c=d' % frame.token})[0] == 200
    assert call(frame, path, token=False, headers={'Cookie': 'pf_token=wrong'})[0] == 401


def test_every_page_and_data_address_answers(frame):
    """With the token, nothing a page loads may crash the server."""
    expected_other = {'/api/qrcode': 400,            # needs ?text=
                      '/api/eink/preview.png': 404}  # no preview on an LCD frame
    bad = []
    for path in get_paths():
        status, _ = call(frame, path)
        if status != expected_other.get(path, 200):
            bad.append('%s -> %s' % (path, status))
    assert not bad, 'unexpected answers:\n  ' + '\n  '.join(bad)


def test_the_kiosk_on_the_frame_itself_needs_no_token(tmp_path):
    f = start_frame(tmp_path, remote=False)
    try:
        assert call(f, '/api/library', token=False)[0] == 200
    finally:
        f.stop()


def test_unticking_a_picture_goes_through_to_the_lists(frame):
    status, body = call(frame, '/api/library')
    data = json.loads(body)
    assert status == 200 and data['total'] == data['shown'] >= 5
    newest = data['items'][0]['name']
    status, body = call(frame, '/api/library', {'names': [newest], 'hidden': True})
    assert status == 200 and json.loads(body)['shown'] == data['total'] - 1
    listed = json.loads(call(frame, '/list')[1])
    assert not any(newest in url for url in listed)
    assert call(frame, '/api/library', {'names': newest, 'hidden': True})[0] == 400
    assert call(frame, '/api/library/thumb/horizontal/..%2Fhidden.json')[0] == 404
    assert call(frame, '/api/library/thumb/0/' + newest)[0] == 404


@pytest.mark.parametrize('endpoint', ['/api/orientation/rotate', '/orientation'])
def test_every_way_of_rotating_also_rotates_the_boot_logo(frame, endpoint, monkeypatch):
    """The phone and the touch menu use one endpoint, an older page another.
    Only one of them used to rebuild the boot splash."""
    from frame import routes
    ran = []

    class Done:
        returncode, stdout, stderr = 0, '', ''

    def fake_run(cmd, *a, **k):
        ran.append([str(c) for c in cmd])
        return Done()

    monkeypatch.setattr(routes.subprocess, 'run', fake_run)
    status, _ = call(frame, endpoint, {'orientation': 'vertical'})
    assert status == 200
    deadline = time.time() + 10
    while time.time() < deadline and not any('install_bootsplash.sh' in ' '.join(c) for c in ran):
        time.sleep(0.1)
    splash = [c for c in ran if 'install_bootsplash.sh' in ' '.join(c)]
    assert splash, 'boot splash not rebuilt; ran: %s' % ran
    assert 'vertical' in splash[0] and splash[0][:2] == ['sudo', '-n']
    assert frame.config()['frame']['orientation'] == 'vertical'
    assert any(c[:3] == ['sudo', 'systemctl', 'restart'] for c in ran)
