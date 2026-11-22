"""The night schedule survives a reboot (it used to live in /tmp)."""
import json

import yaml

from frame.energy import EnergySaveManager


class App:
    def __init__(self, path):
        self.config_path = str(path)
        self.config = yaml.safe_load(open(path)) or {}


def make(tmp_path, cfg, monkeypatch, legacy=None):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.dump(cfg))
    legacy_file = tmp_path / 'frame-schedule.json'
    if legacy is not None:
        legacy_file.write_text(json.dumps(legacy))
    monkeypatch.setattr(EnergySaveManager, 'SCHEDULE_FILE', str(legacy_file))
    return EnergySaveManager(App(path)), path


def test_schedule_is_stored_in_the_config_and_read_back(tmp_path, monkeypatch):
    es, path = make(tmp_path, {'frame': {'name': 'x'}, 'energy_save': {'method': 'ddcci'}},
                    monkeypatch)
    assert es.enabled is False
    es.enabled, es.off_time, es.on_time = True, '21:30', '06:45'
    es._save()
    saved = yaml.safe_load(open(path))
    assert saved['frame'] == {'name': 'x'} and saved['energy_save']['method'] == 'ddcci'
    assert saved['energy_save']['schedule'] == {
        'enabled': True, 'off_time': '21:30', 'on_time': '06:45',
        'weekdays': [0, 1, 2, 3, 4, 5, 6]}
    # "reboot": a new manager, /tmp is empty
    again = EnergySaveManager(App(path))
    assert (again.enabled, again.off_time, again.on_time) == (True, '21:30', '06:45')


def test_the_old_tmp_file_is_migrated_once(tmp_path, monkeypatch):
    es, path = make(tmp_path, {'energy_save': {'method': 'ddcci'}}, monkeypatch,
                    legacy={'enabled': True, 'off_time': '23:00', 'on_time': '08:00',
                            'weekdays': [1, 2, 3]})
    assert (es.enabled, es.off_time, es.weekdays) == (True, '23:00', [1, 2, 3])
    assert yaml.safe_load(open(path))['energy_save']['schedule']['off_time'] == '23:00'


def test_the_config_wins_over_a_stale_tmp_file(tmp_path, monkeypatch):
    es, _ = make(tmp_path, {'energy_save': {'schedule': {'enabled': False, 'off_time': '20:00'}}},
                 monkeypatch, legacy={'enabled': True, 'off_time': '23:00'})
    assert es.enabled is False and es.off_time == '20:00'
