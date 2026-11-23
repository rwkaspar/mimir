"""The slideshow's first-run gate and the config merge used at first boot."""
import subprocess
import sys

import yaml

from frame import services
from frame.slideshow import NativeSlideshow


def show(tmp_path, text):
    p = tmp_path / 'config.yaml'
    p.write_text(text)
    return NativeSlideshow(str(tmp_path), str(p))


def test_setup_state(tmp_path):
    assert show(tmp_path, 'setup_complete: true\n')._setup_state() == 'complete'
    assert show(tmp_path, 'setup_complete: false\n')._setup_state() == 'incomplete'
    assert show(tmp_path, 'frame: {name: x}\n')._setup_state() == 'incomplete'


def test_a_torn_or_missing_config_never_triggers_setup(tmp_path):
    s = show(tmp_path, '')
    assert s._setup_state() == 'unknown' and s._setup_needed() is False
    s.config_path = str(tmp_path / 'missing.yaml')
    assert s._setup_needed() is False
    assert show(tmp_path, 'setup_complete: true\n')._setup_needed() is False
    assert show(tmp_path, 'setup_complete: false\n')._setup_needed() is True


def test_variant_and_display_service():
    assert services.variant({}) == 'lcd'
    assert services.variant({'variant': 'eink'}) == 'eink'
    assert services.display_service({}) == 'photo_frame_slideshow'
    assert services.display_service({'variant': 'eink'}) == 'photo_frame_eink'


def test_merge_config_deep_merges(tmp_path):
    cfg = tmp_path / 'c.yaml'
    over = tmp_path / 'o.yaml'
    cfg.write_text(yaml.dump({'variant': 'lcd', 'frame': {'name': 'a', 'orientation': 'horizontal'},
                              'photos': {'horizontal': {'width': 1920, 'height': 1200}}}))
    over.write_text(yaml.dump({'variant': 'eink', 'frame': {'name': 'kueche'},
                               'eink': {'driver': 'waveshare:epd7in3f'}}))
    r = subprocess.run([sys.executable, 'scripts/merge_config.py', str(cfg), str(over)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = yaml.safe_load(cfg.read_text())
    assert out['variant'] == 'eink'
    assert out['frame'] == {'name': 'kueche', 'orientation': 'horizontal'}
    assert out['eink']['driver'] == 'waveshare:epd7in3f'
    assert out['photos']['horizontal']['width'] == 1920


def test_sudo_refusal_is_reported_as_config_fault_not_as_dead_panel(monkeypatch, caplog):
    """`sudo: a password is required` used to be read as "panel unreachable"
    and sent everyone hunting for a flaky DDC bus."""
    import types
    from frame import energy

    def fake_run(cmd, **kw):
        assert cmd[:3] == ['sudo', '-n', 'ddcutil']      # never prompt
        return types.SimpleNamespace(stdout='', returncode=1,
                                     stderr='sudo: Ein Passwort ist notwendig\n')
    monkeypatch.setattr(energy.subprocess, 'run', fake_run)
    monkeypatch.setattr(energy.EnergySaveManager, '_sudo_warned', False)
    es = energy.EnergySaveManager()
    with caplog.at_level('ERROR'):
        assert es._display_is_on() is None
        assert es._display_is_on() is None
    msgs = [r.message for r in caplog.records if 'sudo refused' in r.message]
    assert len(msgs) == 1 and 'NOPASSWD' in msgs[0]


def test_clock_overlay_for_video(tmp_path):
    """Over video the clock is a cropped RGBA picture for its own DRM plane;
    the video pipeline itself stays untouched (zero-copy)."""
    for orient in ('horizontal', 'vertical'):
        s = show(tmp_path, 'setup_complete: true\nframe: {orientation: %s}\n'
                           'clock: {enabled: true, size: l, position: br}\n' % orient)
        im, x, y = s._clock_overlay_image()
        assert im.mode == 'RGBA' and im.getbbox() == (0, 0) + im.size
        assert x + im.size[0] <= s.fb_w and y + im.size[1] <= s.fb_h
        assert im.size[0] * im.size[1] < s.fb_w * s.fb_h / 8   # small, not a full frame
        assert s._video_pipeline_desc('/x/a.mp4').endswith(
            'v4l2h264dec ! kmssink force-modesetting=true')
        assert s._video_pipeline_desc('/x/a.mp4', 7).endswith(
            'kmssink force-modesetting=true fd=7')
    off = show(tmp_path, 'setup_complete: true\nclock: {enabled: false}\n')
    assert off._clock_overlay_image() is None
    assert off._VideoClock(off).fd is None        # clock off: card is never opened
    broken = show(tmp_path, 'setup_complete: true\nclock: {enabled: true}\n')
    broken._overlay_broken = True
    assert broken._VideoClock(broken).fd is None
    quiet = show(tmp_path, 'setup_complete: true\nclock: {enabled: true}\n')
    c = quiet._VideoClock(quiet, want=False)
    assert c.fd is None
    c.tick(None); c.draw(); c.close()             # no-ops without an fd


def test_drm_plane_pixels():
    """KMS wants B,G,R,A bytes with colour premultiplied by alpha."""
    from PIL import Image
    from frame.display import drm_plane
    im = Image.new('RGBA', (2, 1))
    im.putpixel((0, 0), (255, 128, 0, 255))
    im.putpixel((1, 0), (255, 255, 255, 128))
    assert drm_plane.premultiplied_bgra(im) == bytes(
        [0, 128, 255, 255, 128, 128, 128, 128])


def test_power_on_qr_only_without_touch(tmp_path, monkeypatch):
    """`frame.remote_qr_on_boot`: auto = only when no touchscreen is attached."""
    class UI:
        touch = False

        @classmethod
        def touch_present(cls):
            return cls.touch
    auto = show(tmp_path, 'setup_complete: true\n')
    auto._native_ui = UI
    assert auto._boot_qr_wanted() is True
    UI.touch = True
    assert auto._boot_qr_wanted() is False
    always = show(tmp_path, 'setup_complete: true\nframe: {remote_qr_on_boot: always}\n')
    always._native_ui = UI
    assert always._boot_qr_wanted() is True
    UI.touch = False
    never = show(tmp_path, 'setup_complete: true\nframe: {remote_qr_on_boot: never}\n')
    never._native_ui = UI
    assert never._boot_qr_wanted() is False


def test_fb0_under_a_video_holds_the_next_photo_with_the_current_time(tmp_path, monkeypatch):
    """When a clip ends, fb0 shows through for a moment. It must then hold the
    photo that follows with the current time — not the photo from before the
    clip with the time of back then."""
    import time as _time
    s = show(tmp_path, 'clock: {enabled: true}\n')
    calls = []
    monkeypatch.setattr(s, '_write_fb', lambda raw: calls.append(('photo', raw)))
    monkeypatch.setattr(s, '_overlay_clock', lambda raw, cfg=None: calls.append(('clock', raw)))
    now = [_time.mktime((2026, 11, 23, 10, 15, 30, 0, 0, -1))]
    monkeypatch.setattr('frame.slideshow.time.localtime',
                        lambda *a: _time.gmtime(now[0]) if not a else _time.gmtime(*a))
    state = {}
    s._refresh_under_video('/tmp/next.raw', state)
    assert calls == [('photo', '/tmp/next.raw'), ('clock', '/tmp/next.raw')]
    s._refresh_under_video('/tmp/next.raw', state)        # same minute: nothing
    assert len(calls) == 2
    now[0] += 60                                          # minute rolls over
    s._refresh_under_video('/tmp/next.raw', state)
    assert calls[2:] == [('clock', '/tmp/next.raw')]
    calls.clear()
    s._refresh_under_video(None, {})                      # nothing to put there
    assert calls == []

    off = show(tmp_path, 'clock: {enabled: false}\n')
    monkeypatch.setattr(off, '_write_fb', lambda raw: calls.append(('photo', raw)))
    st = {}
    off._refresh_under_video('/tmp/next.raw', st)
    now[0] += 60
    off._refresh_under_video('/tmp/next.raw', st)
    assert calls == [('photo', '/tmp/next.raw')]          # once, no clock
