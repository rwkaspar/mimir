#!/usr/bin/env python3
"""Read-only health check, run ON a frame after every deploy:

    ssh frame@<frame> python3 digital_photo_frame/scripts/frame_check.py

It answers "is what I just deployed really working on this device?" without
changing anything: only GET requests to the local server, and reads of
systemd, the journal, sudo's rule list and files. It never POSTs.

It exists because "deployed" was reported several times without anyone
checking the device: a missing sudoers rule (the frame did not wake), a
schedule lost at reboot, thumbnails that were all 404, a boot logo in the
wrong orientation. Each of those is a line here.

Exit code 0 = all checks passed, 1 = at least one FAIL.
Standard library only; works with the system python3.
"""
import filecmp
import json
import os
import re
import subprocess
import sys
import urllib.request

REPO = os.environ.get('MIMIR_REPO') or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
BASE = 'http://127.0.0.1:%s' % os.environ.get('PORT', '8080')
results = []


def report(ok, title, detail=''):
    results.append(ok)
    mark = {True: ' ok ', False: 'FAIL', None: 'note'}[ok]
    print('[%s] %s%s' % (mark, title, (' — ' + detail) if detail else ''))


def sh(*cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def get(path, timeout=30):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b''
    except Exception as e:
        return 0, str(e).encode()


def get_json(path):
    status, body = get(path)
    try:
        return status, json.loads(body)
    except Exception:
        return status, None


def load_config():
    """The few values we need, without PyYAML (system python may lack it)."""
    path = os.path.join(REPO, 'config_frame.yaml')
    text = open(path).read() if os.path.exists(path) else ''
    m = re.search(r'^variant:\s*(\w+)', text, re.M)
    return {'variant': m.group(1) if m else 'lcd', 'text': text}


def check_version():
    code, head = sh('git', '-C', REPO, 'rev-parse', '--short', 'HEAD')
    _, remote = sh('git', '-C', REPO, 'rev-parse', '--short', 'origin/main')
    _, subject = sh('git', '-C', REPO, 'log', '-1', '--format=%s')
    report(code == 0 and head == remote, 'code is at origin/main as last fetched',
           '%s "%s"%s' % (head, subject[:50], '' if head == remote else ' (origin/main: %s)' % remote))
    _, dirty = sh('git', '-C', REPO, 'status', '--porcelain', '--untracked-files=no')
    report(not dirty, 'no local changes to tracked files', dirty.replace('\n', ', ')[:120])


def check_services(variant):
    display = 'photo_frame_eink' if variant == 'eink' else 'photo_frame_slideshow'
    _, sleeping = get_json('/schedule')
    asleep = bool(sleeping and sleeping.get('sleeping'))
    for unit in ('photo_frame_server', display):
        _, state = sh('systemctl', 'is-active', unit)
        if unit == display and (asleep or variant == 'eink'):
            report(None, '%s is %s' % (unit, state),
                   'asleep' if asleep else 'e-ink runs once per wake')
            continue
        report(state == 'active', '%s is running' % unit, state)
        stale = stale_files(unit, ENTRY[unit])
        if stale is None:
            report(None, '%s: could not tell which code it was started with' % unit)
        else:
            report(not stale, '%s runs the code that is checked out' % unit,
                   'started before these changed, RESTART it: ' + ', '.join(stale)[:160]
                   if stale else '')


ENTRY = {'photo_frame_server': 'frame/server.py',
         'photo_frame_slideshow': 'frame/slideshow.py',
         'photo_frame_eink': 'frame/eink_cycle.py'}


def module_closure(entry):
    """Every frame/*.py that `entry` imports, directly or not."""
    import ast
    seen, todo = set(), [entry]
    while todo:
        rel = todo.pop()
        path = os.path.join(REPO, rel)
        if rel in seen or not os.path.exists(path):
            continue
        seen.add(rel)
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module] + ['%s.%s' % (node.module, a.name) for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for m in mods:
                if m == 'frame' or m.startswith('frame.'):
                    base = m.replace('.', '/')
                    todo += [base + '.py', base + '/__init__.py']
    return seen


def stale_files(unit, entry):
    """Source files of this service that changed after it was started — the
    process still runs the old version of them. None if unknown."""
    _, started = sh('systemctl', 'show', unit, '-p', 'ActiveEnterTimestamp', '--value')
    code, epoch = sh('date', '-d', started, '+%s')
    if code != 0 or not epoch.isdigit():
        return None
    # The commit that was checked out when the service started, from the reflog.
    code, log = sh('git', '-C', REPO, 'log', '-g', '--format=%H %gd', '--date=unix', 'HEAD')
    if code != 0:
        return None
    then = None
    for line in log.splitlines():                  # newest first
        m = re.match(r'(\w+) HEAD@\{(\d+)\}', line)
        if m and int(m.group(2)) <= int(epoch):
            then = m.group(1)
            break
    if then is None:
        return None
    _, changed = sh('git', '-C', REPO, 'diff', '--name-only', then, 'HEAD')
    mine = module_closure(entry)
    return sorted(f for f in changed.splitlines() if f in mine)


def check_pages():
    bad = []
    for path in ('/remote', '/library', '/wizard', '/setup', '/api/frame/settings',
                 '/schedule', '/orientation', '/sysinfo', '/api/version', '/api/access',
                 '/sync/status', '/api/wifi/status', '/api/display', '/list'):
        status, _ = get(path)
        if status != 200:
            bad.append('%s -> %s' % (path, status or 'no answer'))
    report(not bad, 'pages and data addresses answer', ', '.join(bad))


def check_library():
    status, data = get_json('/api/library')
    if status != 200 or not data:
        report(False, 'pictures list', 'HTTP %s' % status)
        return
    bad = []
    for item in data['items']:
        s, body = get('/api/library/thumb/%s/%s' % (data['orientation'],
                                                    urllib.request.quote(item['name'])), 60)
        if s != 200 or len(body) < 500:
            bad.append(item['name'])
    report(not bad, 'every picture has a thumbnail (%d pictures, %d in the slideshow)'
           % (data['total'], data['shown']), ', '.join(bad)[:200])
    report(data['total'] > 0, 'the frame has pictures for "%s"' % data['orientation'])


def check_schedule(cfg):
    status, sched = get_json('/schedule')
    if status != 200 or sched is None:
        report(False, 'night schedule readable', 'HTTP %s' % status)
        return
    in_config = bool(re.search(r'^energy_save:\s*\n(?:[ \t]+.*\n)*?[ \t]+schedule:', cfg['text'], re.M))
    if sched.get('enabled'):
        report(in_config, 'night schedule is stored in the config (survives a power cut)',
               '%s–%s' % (sched.get('off_time'), sched.get('on_time')))
    else:
        report(None, 'night schedule is off')


def required_rules():
    """NOPASSWD rules setup_pi.sh would install, as sudo -l prints them."""
    text = open(os.path.join(REPO, 'scripts', 'setup_pi.sh')).read()
    rules = []
    for line in text.splitlines():
        m = re.match(r'\$\{FRAME_USER\} ALL=\(ALL\) NOPASSWD: (.+)$', line.strip())
        if m:
            rules.append(m.group(1))
    return rules


def check_sudo(variant):
    code, listing = sh('sudo', '-n', '-l')
    if 'NOPASSWD' not in listing:
        report(False, 'sudo rules readable', listing[:120])
        return
    have = [l.split('NOPASSWD:', 1)[1].strip() for l in listing.splitlines() if 'NOPASSWD:' in l]
    have_cmds = {(h.split(' ', 1)[0].rsplit('/', 1)[-1], h.split(' ', 1)[1] if ' ' in h else '')
                 for h in have}
    missing = []
    for rule in required_rules():
        if 'eink' in rule and variant != 'eink':
            continue
        if 'slideshow' in rule and variant == 'eink':
            continue
        rule = rule.replace('${IPTABLES_BIN}', 'iptables').replace('${DDCUTIL_BIN}', 'ddcutil')
        rule = rule.replace('${REPO_DIR}', REPO)
        cmd, _, args = rule.partition(' ')
        name = cmd.rsplit('/', 1)[-1]
        if (name, args) in have_cmds or (name, '') in have_cmds:
            continue
        if name in ('tailscale', 'pkill') and sh('which', 'tailscale')[0] != 0:
            continue                              # Tailscale not installed here
        missing.append(rule.replace(REPO + '/', ''))
    report(not missing, 'every privileged command the code uses has its sudo rule',
           'MISSING: ' + '; '.join(missing) if missing else '%d rules' % len(have))


def check_splash(cfg):
    if cfg['variant'] == 'eink':
        return
    _, o = get_json('/orientation')
    orientation = (o or {}).get('orientation', 'horizontal')
    installed = '/usr/share/plymouth/themes/photoframe/anim'
    source = os.path.join(REPO, 'imaging', 'plymouth', 'photoframe', 'anim', orientation)
    try:
        frames = sorted(os.listdir(source))
        last = frames[-1]
        same = filecmp.cmp(os.path.join(source, last), os.path.join(installed, last), shallow=False)
        report(same, 'boot logo matches the orientation (%s)' % orientation,
               '' if same else 'installed splash is for the other orientation or outdated')
    except Exception as e:
        report(False, 'boot logo installed', str(e)[:120])


def check_journal(variant):
    display = 'photo_frame_eink' if variant == 'eink' else 'photo_frame_slideshow'
    for unit in ('photo_frame_server', display):
        _, pid_start = sh('systemctl', 'show', unit, '-p', 'ActiveEnterTimestamp', '--value')
        args = ['journalctl', '-u', unit, '--no-pager', '-o', 'cat']
        args += ['--since', pid_start.split(' ', 1)[1].rsplit(' ', 1)[0]] if pid_start else ['-b']
        _, log = sh(*args, timeout=60)
        lines = log.splitlines()
        errors = [l for l in lines if re.search(r'\bERROR\b|Traceback|\bCRITICAL\b', l)]
        warnings = [l for l in lines if re.search(r'\bWARNING\b', l)
                    and 'TouchReader' not in l]
        touch = sum(1 for l in lines if 'TouchReader' in l)
        report(not errors, '%s: no errors in the log since it started' % unit,
               '%d error line(s), last: %s' % (len(errors), errors[-1][-160:]) if errors else '')
        if warnings:
            kinds = {}
            for w in warnings:
                key = re.sub(r'\d+', 'N', w.split('WARNING', 1)[1])[:90]
                kinds[key] = kinds.get(key, 0) + 1
            top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
            report(None, '%s: %d warning(s)' % (unit, len(warnings)),
                   '; '.join('%dx%s' % (n, k) for k, n in top))
        if touch:
            report(None, '%s: touch controller not found (%d retries)' % (unit, touch))


def check_power():
    _, out = sh('vcgencmd', 'get_throttled')
    m = re.search(r'0x([0-9a-fA-F]+)', out)
    if m:
        bits = int(m.group(1), 16)
        report(not (bits & 0x1), 'power supply holds (no under-voltage right now)', out)
        if bits & 0x10000:
            report(None, 'under-voltage has occurred since boot', out)
    _, boots = sh('journalctl', '--list-boots', '--no-pager')
    report(None, 'boots on record: %d' % len([l for l in boots.splitlines() if l.strip()]))


def main():
    cfg = load_config()
    status, _ = get('/api/version', 10)
    if status != 200:
        report(False, 'server answers on %s' % BASE, 'HTTP %s' % (status or 'no answer'))
        check_version()
        return 1
    check_version()
    check_services(cfg['variant'])
    check_pages()
    check_library()
    check_schedule(cfg)
    check_sudo(cfg['variant'])
    check_splash(cfg)
    check_journal(cfg['variant'])
    check_power()
    fails = sum(1 for r in results if r is False)
    print('\n%d check(s), %d failed' % (len([r for r in results if r is not None]), fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
