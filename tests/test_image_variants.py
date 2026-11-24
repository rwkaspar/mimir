"""One image per display (HDMI, and each e-ink panel), from one bake script.

The pieces live in four files that only meet on a CI runner and on a frame's
first boot: the workflow rewrites defaults in the bake script with sed, the
bake script leaves a file for setup_pi.sh, setup_pi.sh turns it into the
config the wizard reads. These tests hold the seams together.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
BAKE = REPO / 'imaging/stage-photoframe/00-photoframe/01-run-chroot.sh'
WORKFLOW = REPO / '.github/workflows/build-image.yml'
SETUP = REPO / 'scripts/setup_pi.sh'


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())['jobs']['build-image']


def test_matrix_builds_hdmi_and_every_panel_the_wizard_knows():
    """A panel without an image would start with an empty panel."""
    from frame import display_choice as dc
    rows = workflow()['strategy']['matrix']['include']
    built = {(r['variant'], r['driver'], r['name']) for r in rows}
    want = {('lcd', '', dc.image_name())}
    want |= {('eink', d, dc.image_name(d)) for d, _ in dc.EINK_PANELS}
    assert built == want
    assert len({r['name'] for r in rows}) == len(rows)          # distinct file names


def packages(name):
    return set((BAKE.parent / name).read_text().split())


@pytest.mark.parametrize('variant', ['lcd', 'eink'])
def test_image_has_every_package_first_boot_wants(variant):
    """Anything missing is fetched by setup_pi.sh on first boot — which needs
    a network the frame may not have yet."""
    text = SETUP.read_text()
    def listed(var):
        return set(re.search(r'^%s="([^"]+)"' % var, text, re.M).group(1).split())
    want = listed('PACKAGES_COMMON') | listed('PACKAGES_' + variant.upper())
    have = packages('00-packages') | packages('packages-' + variant)
    assert not want - have, sorted(want - have)


def test_each_image_carries_only_its_own_display_software():
    assert not packages('00-packages') & (packages('packages-lcd') | packages('packages-eink'))
    assert 'python3-gi' not in packages('packages-eink')          # no video stack on e-ink
    assert 'python3-spidev' not in packages('packages-lcd')       # no panel/battery parts on HDMI


def test_every_sed_in_the_workflow_hits_the_bake_script_once():
    """A sed that matches nothing is silent: the image would be built from the
    defaults (wrong ref, or both images as HDMI)."""
    bake = BAKE.read_text()
    pin = next(s['run'] for s in workflow()['steps'] if 'bake script' in s.get('name', ''))
    patterns = re.findall(r'sed -i "s\|(.+?)\|', pin)
    assert len(patterns) == 4
    for pat in patterns:
        assert bake.count(pat.replace('\\.', '.')) == 1, pat


def bake_with(variant, tmp_path, driver=''):
    """Run the variant part of the bake script against a fake root."""
    text = BAKE.read_text()
    start = text.index('# ---- Variant ----')
    end = text.index('# i2c-dev is needed')
    head = text[:text.index('BOOT_DIR=')]
    boot = tmp_path / 'boot'
    (boot / 'photo_frame/scripts').mkdir(parents=True, exist_ok=True)
    (boot / 'config.txt').write_text('# stock\n#dtparam=spi=on\n')
    (boot / 'photo_frame/scripts/install_eink_libs.sh').write_text(
        'echo "libs $1" > "%s"\n' % (tmp_path / 'libs'))
    script = (head + 'BOOT_DIR="%s"\nBOOT_REPO="${BOOT_DIR}/photo_frame"\n' % boot
              + text[start:end]).replace('/etc/mimir', str(tmp_path / 'etc'))
    return subprocess.run(['bash', '-e', '-c', script], capture_output=True, text=True,
                          env=dict(os.environ, PHOTOFRAME_VARIANT=variant,
                                   PHOTOFRAME_EINK_DRIVER=driver)), boot


@pytest.mark.parametrize('variant,driver,lib', [
    ('lcd', '', None),
    ('eink', 'waveshare:epd7in3e', 'waveshare'),
    ('eink', 'inky', 'inky'),
])
def test_bake_leaves_display_and_panel_for_first_boot(variant, driver, lib, tmp_path):
    r, boot = bake_with(variant, tmp_path, driver)
    assert r.returncode == 0, r.stderr
    image = yaml.safe_load((tmp_path / 'etc/image.yaml').read_text())
    config_txt = (boot / 'config.txt').read_text()
    if variant == 'eink':
        assert image == {'variant': 'eink', 'variant_fixed': True, 'eink': {'driver': driver}}
        assert re.search(r'^dtparam=spi=on$', config_txt, re.M)
        assert re.search(r'^dtparam=i2c_arm=on$', config_txt, re.M)
        assert (tmp_path / 'libs').read_text().strip() == 'libs ' + lib
    else:
        assert image == {'variant': 'lcd', 'variant_fixed': True}
        assert not re.search(r'^dtparam=spi=on$', config_txt, re.M)
        assert not (tmp_path / 'libs').exists()


def test_bake_refuses_an_unknown_variant_or_an_eink_image_without_panel(tmp_path):
    assert bake_with('oled', tmp_path)[0].returncode != 0
    assert bake_with('eink', tmp_path)[0].returncode != 0
    assert bake_with('eink', tmp_path, 'something')[0].returncode != 0


def read_variant(tmp_path, config=None, override=None, image=None):
    """setup_pi.sh's own read_variant, run on files we control."""
    text = SETUP.read_text()
    fn = text[text.index('read_variant() {'):]
    fn = fn[:fn.index('\n}\n') + 3]
    paths = {}
    for name, content in (('config_frame.yaml', config), ('mimir.yaml', override),
                          ('image.yaml', image)):
        paths[name] = tmp_path / name
        if content is not None:
            paths[name].write_text(content)
    script = ('REPO_DIR="%s"\nBOOT_OVERRIDE="%s"\nIMAGE_DEFAULTS="%s"\n%sread_variant\n'
              % (tmp_path, paths['mimir.yaml'], paths['image.yaml'], fn))
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True).stdout.strip()


def test_first_boot_takes_the_variant_from_the_image(tmp_path):
    assert read_variant(tmp_path) == 'lcd'
    assert read_variant(tmp_path, image='variant: eink\nvariant_fixed: true\n') == 'eink'
    # the owner's file on the boot partition beats the image …
    assert read_variant(tmp_path, override='variant: lcd\n',
                        image='variant: eink\n') == 'lcd'
    # … and a file that says nothing about the display leaves the image's choice
    assert read_variant(tmp_path, override='frame:\n  name: kitchen\n',
                        image='variant: eink\n') == 'eink'
    # an existing config (a frame switched by hand) beats both
    assert read_variant(tmp_path, config='variant: lcd\n', image='variant: eink\n') == 'lcd'


def test_setup_reads_the_file_the_bake_writes():
    assert 'cat > /etc/mimir/image.yaml' in BAKE.read_text()
    assert 'IMAGE_DEFAULTS="/etc/mimir/image.yaml"' in SETUP.read_text()


def test_image_defaults_merge_into_a_new_config(tmp_path):
    cfg = tmp_path / 'config_frame.yaml'
    cfg.write_text('setup_complete: false\nvariant: lcd\nframe:\n  name: photo-frame\n')
    image = tmp_path / 'image.yaml'
    image.write_text('variant: eink\nvariant_fixed: true\neink:\n  driver: "inky"\n')
    subprocess.run(['python3', str(REPO / 'scripts/merge_config.py'), str(cfg), str(image)],
                   check=True, capture_output=True)
    merged = yaml.safe_load(cfg.read_text())
    assert merged['variant'] == 'eink' and merged['variant_fixed'] is True
    assert merged['frame'] == {'name': 'photo-frame'} and merged['eink'] == {'driver': 'inky'}


def test_panel_libraries_are_not_fetched_twice(tmp_path):
    """On an e-ink image the library is there: first boot must not need the
    network for it."""
    (tmp_path / 'waveshare_epd').mkdir()
    (tmp_path / 'inky').mkdir()
    r = subprocess.run(['bash', str(REPO / 'scripts/install_eink_libs.sh'), 'all'],
                       capture_output=True, text=True,
                       env=dict(os.environ, MIMIR_EINK_LIB_DIR=str(tmp_path), PATH='/usr/bin:/bin'))
    assert r.returncode == 0 and r.stdout.count('already installed') == 2, r.stdout + r.stderr
