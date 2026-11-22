"""The wizard's "which display?" question (frame/display_choice.py)."""
import pytest
import yaml

from frame import display_choice as dc


def cfg_file(tmp_path, cfg):
    p = tmp_path / 'config.yaml'
    p.write_text(yaml.dump(cfg))
    return str(p)


def test_hdmi_detection(tmp_path):
    for name, text in (('card0-HDMI-A-1', 'disconnected\n'), ('card0-HDMI-A-2', 'connected\n')):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'status').write_text(text)
    assert dc.hdmi_connected(str(tmp_path / 'card*-HDMI-A-*' / 'status')) is True
    (tmp_path / 'card0-HDMI-A-2' / 'status').write_text('disconnected\n')
    assert dc.hdmi_connected(str(tmp_path / 'card*-HDMI-A-*' / 'status')) is False
    assert dc.hdmi_connected(str(tmp_path / 'nothing*')) is None


def test_suggestion_follows_hdmi_then_the_config():
    assert dc.options({}, hdmi=True, switchable=True)['suggested'] == 'lcd'
    assert dc.options({}, hdmi=False, switchable=True)['suggested'] == 'eink'
    o = dc.options({'variant': 'eink', 'eink': {'driver': 'waveshare:epd7in3e'}},
                   hdmi=dc.hdmi_connected('/nonexistent/*'), switchable=False)
    assert o['suggested'] == 'eink' and o['driver'] == 'waveshare:epd7in3e'
    assert o['confirmed'] is False and o['can_switch'] is False
    assert any(p['driver'] == 'waveshare:epd7in3e' for p in o['panels'])


def test_every_offered_panel_is_known_to_the_pipeline():
    from frame.display import eink
    for driver, _ in dc.EINK_PANELS:
        assert driver == 'inky' or driver.split(':', 1)[1] in eink.PANELS


def test_same_variant_is_only_confirmed(tmp_path):
    cfg = {'variant': 'lcd', 'setup_complete': False}
    path = cfg_file(tmp_path, cfg)
    ran = []
    res = dc.apply(cfg, path, {'variant': 'lcd'}, run_switch=ran.append)
    assert res == {'ok': True, 'restart': False, 'variant': 'lcd'} and ran == []
    saved = yaml.safe_load(open(path))
    assert saved['display_confirmed'] is True and saved['variant'] == 'lcd'
    assert dc.options(cfg, hdmi=True, switchable=True)['confirmed'] is True


def test_choosing_eink_stores_the_panel_and_switches(tmp_path):
    cfg = {'variant': 'lcd', 'frame': {'name': 'x'}}
    path = cfg_file(tmp_path, cfg)
    ran = []
    res = dc.apply(cfg, path, {'variant': 'eink', 'driver': 'waveshare:epd7in3e',
                               'pisugar_model': 'PiSugar 3'}, run_switch=ran.append)
    assert res['restart'] is True and ran == ['eink']
    saved = yaml.safe_load(open(path))
    assert saved['variant'] == 'eink' and saved['frame'] == {'name': 'x'}
    assert saved['eink'] == {'driver': 'waveshare:epd7in3e', 'pisugar_model': 'PiSugar 3'}
    assert cfg['variant'] == 'eink'            # a later save_config keeps the choice


def test_changing_the_panel_or_battery_also_switches(tmp_path):
    cfg = {'variant': 'eink', 'eink': {'driver': 'waveshare:epd7in3f',
                                       'pisugar_model': 'PiSugar 3', 'update_time': '07:00'}}
    path = cfg_file(tmp_path, cfg)
    ran = []
    dc.apply(cfg, path, {'variant': 'eink', 'driver': 'waveshare:epd7in3f',
                         'pisugar_model': 'PiSugar 3'}, run_switch=ran.append)
    assert ran == []
    dc.apply(cfg, path, {'variant': 'eink', 'driver': 'waveshare:epd7in3e',
                         'pisugar_model': ''}, run_switch=ran.append)
    assert ran == ['eink']
    saved = yaml.safe_load(open(path))['eink']
    assert saved == {'driver': 'waveshare:epd7in3e', 'update_time': '07:00'}


def test_back_to_lcd(tmp_path):
    cfg = {'variant': 'eink', 'eink': {'driver': 'inky'}}
    path = cfg_file(tmp_path, cfg)
    ran = []
    assert dc.apply(cfg, path, {'variant': 'lcd'}, run_switch=ran.append)['restart']
    assert ran == ['lcd'] and yaml.safe_load(open(path))['variant'] == 'lcd'


def test_bad_answers_are_rejected(tmp_path):
    cfg = {'variant': 'lcd'}
    path = cfg_file(tmp_path, cfg)
    for data in ({'variant': 'crt'}, {'variant': 'eink', 'driver': 'rm -rf'},
                 {'variant': 'eink', 'driver': 'inky', 'pisugar_model': 'x; reboot'}):
        with pytest.raises(ValueError):
            dc.apply(cfg, path, data, run_switch=lambda v: None)
    assert yaml.safe_load(open(path)) == {'variant': 'lcd'}


def test_without_the_root_helper_nothing_is_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, 'can_switch', lambda: False)
    cfg = {'variant': 'lcd'}
    path = cfg_file(tmp_path, cfg)
    res = dc.apply(cfg, path, {'variant': 'eink', 'driver': 'inky'})
    assert res == {'ok': False, 'error': 'cannot_switch', 'restart': False}
    assert yaml.safe_load(open(path)) == {'variant': 'lcd'} and cfg == {'variant': 'lcd'}
