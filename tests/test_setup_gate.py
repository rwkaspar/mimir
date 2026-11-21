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
