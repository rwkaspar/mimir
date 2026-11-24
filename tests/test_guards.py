"""Guards against mistakes that have actually shipped.

Each test here stands for a class of bug that reached a real frame and was
only noticed by its owner. They read the source — no device needed — and
fail with a list of every place that breaks the rule.

  * a privileged command without a sudoers rule fails silently on the device
    (brightness wake, boot-splash rebuild)
  * state kept in /tmp is gone after a power cut (the night schedule)
  * a page calls an address the server does not route, or with the wrong verb
  * a text key without a translation shows up raw on the phone
  * a page script uses a name the browser already owns (`orientation` on
    phones is window.orientation: read-only, 0)
"""
import ast
import fnmatch
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FRAME = REPO / 'frame'
VIEWER = REPO / 'viewer'
PY_FILES = sorted(FRAME.rglob('*.py'))
PAGES = sorted(VIEWER.glob('*.html'))


# --------------------------------------------------------------------------
# 1. Every privileged command has a sudoers rule
# --------------------------------------------------------------------------

DYNAMIC = '\x00'          # an argument only known at run time


def _script_behind(el, tree):
    """A command given as a variable (`str(script)`, SWITCH_SCRIPT): the
    *.sh name it is built from, found in the variable's assignment."""
    names = {n.id for n in ast.walk(el) if isinstance(n, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            for c in ast.walk(node.value):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                        and c.value.endswith('.sh'):
                    return c.value
    return DYNAMIC


def sudo_calls():
    """(file, line, [command, args...]) for every ['sudo', ...] list literal."""
    out = []
    for path in PY_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or not node.elts:
                continue
            first = node.elts[0]
            if not (isinstance(first, ast.Constant) and first.value == 'sudo'):
                continue
            words = []
            for el in node.elts[1:]:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    words.append(el.value)
                elif not [w for w in words if w not in ('-n', '-S')]:
                    words.append(_script_behind(el, tree))     # the command itself
                else:
                    words.append(DYNAMIC)
            while words and words[0] in ('-n', '-S'):
                words.pop(0)
            if words and words[0] != '-l':            # `sudo -l` needs no rule
                out.append((path.relative_to(REPO), node.lineno, words))
    return out


def sudoers_rules():
    """[(command basename, args or None)] from the block setup_pi.sh installs."""
    rules = []
    text = (REPO / 'scripts' / 'setup_pi.sh').read_text()
    for line in text.splitlines():
        m = re.match(r'\$\{FRAME_USER\} ALL=\(ALL\) NOPASSWD: (.+)$', line.strip())
        if not m:
            continue
        spec = m.group(1).replace('\\:', ':')
        spec = spec.replace('${IPTABLES_BIN}', 'iptables').replace('${DDCUTIL_BIN}', 'ddcutil')
        cmd, _, args = spec.partition(' ')
        rules.append((cmd.rsplit('/', 1)[-1], args or None))
    return rules


def covered(words, rules):
    cmd = words[0]
    if cmd == DYNAMIC:
        return False
    name = cmd.rsplit('/', 1)[-1]
    for rule_cmd, rule_args in rules:
        if rule_cmd != name:
            continue
        if rule_args is None:
            return True
        # Rule with arguments: the call's arguments must match it. A run-time
        # argument matches if the rule could be meant for it.
        call = ' '.join('*' if w == DYNAMIC else w for w in words[1:])
        if fnmatch.fnmatchcase(call, rule_args) or fnmatch.fnmatchcase(rule_args, call):
            return True
    return False


# Privileged calls that are known to have no rule. Every entry is a real gap
# on a freshly set-up frame — listed so it is visible, not so it is fine.
# Fix one (add the rule to setup_pi.sh or drop the call) and delete its line.
KNOWN_SUDO_GAPS = {
    # The old browser kiosk (cage/cog). setup_pi.sh no longer installs that
    # service or its rules; this code path is a leftover.
    ('frame/native_video.py', 'plymouth quit'),
    ('frame/native_video.py', 'systemctl stop photo_frame_cage'),
    ('frame/native_video.py', 'systemctl restart photo_frame_cage'),
}


def test_every_sudo_command_has_a_sudoers_rule():
    rules = sudoers_rules()
    assert len(rules) > 10, 'sudoers block in setup_pi.sh not found'
    missing = []
    for path, line, words in sudo_calls():
        if covered(words, rules):
            continue
        shown = ' '.join('<…>' if w == DYNAMIC else w for w in words)
        if (str(path), shown) in KNOWN_SUDO_GAPS:
            continue
        missing.append('%s:%d  sudo %s' % (path, line, shown))
    assert not missing, (
        'These run through sudo but setup_pi.sh installs no NOPASSWD rule for '
        'them, so on a frame they fail without anyone noticing:\n  '
        + '\n  '.join(missing))


def test_known_sudo_gaps_still_exist():
    """A fixed gap must leave the list — otherwise the list rots."""
    rules = sudoers_rules()
    live = {(str(p), ' '.join('<…>' if w == DYNAMIC else w for w in words))
            for p, _, words in sudo_calls() if not covered(words, rules)}
    stale = sorted(set(KNOWN_SUDO_GAPS) - live)
    assert not stale, 'fixed, remove from KNOWN_SUDO_GAPS: %s' % stale


# --------------------------------------------------------------------------
# 2. Nothing that must survive a reboot lives in /tmp
# --------------------------------------------------------------------------

# /tmp is a RAM disk on the frame: empty after every power cut. Each path
# used there needs a reason why losing it is fine.
TMP_OK = {
    '/tmp/native_ui.raw': 'scratch buffer for one menu frame',
    '/tmp/frame_downloads': 'downloads in flight; re-fetched by the next sync',
    '/tmp/slideshow_concat.txt': 'rebuilt whenever the video wall starts',
    '/tmp/slideshow_concat.mp4': 'rebuilt whenever the video wall starts',
    '/tmp/slideshow_ring.raw': 'scratch buffer for the hold ring',
    '/tmp/slideshow_a.raw': 'render buffer for the current photo',
    '/tmp/slideshow_b.raw': 'render buffer for the next photo',
    '/tmp/frame-brightness': 'live sensor reading, rewritten continuously',
    '/tmp/frame-schedule.json': 'legacy location, only read once to migrate',
    '/tmp/frame-runtime': 'XDG runtime dir for a child process',
    '/tmp/mimir_eink.png': 'simulated e-ink output for development',
}


def test_no_lasting_state_in_tmp():
    found = {}
    for path in PY_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.match(r'/tmp/[\w.\-/]+$', node.value)):
                found.setdefault(node.value, '%s:%d' % (path.relative_to(REPO), node.lineno))
    unknown = {p: where for p, where in found.items() if p not in TMP_OK}
    assert not unknown, (
        '/tmp is wiped on every power cut. Keep settings in the config instead, '
        'or add the path to TMP_OK with the reason losing it is fine:\n  '
        + '\n  '.join('%s  (%s)' % kv for kv in sorted(unknown.items())))
    stale = sorted(set(TMP_OK) - set(found))
    assert not stale, 'no longer used, remove from TMP_OK: %s' % stale


# --------------------------------------------------------------------------
# Page scripts, parsed once
# --------------------------------------------------------------------------

esprima = pytest.importorskip('esprima')


def page_scripts(page):
    html = page.read_text()
    return re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)


def parsed(page):
    trees = []
    for src in page_scripts(page):
        try:
            trees.append(esprima.parseScript(src, {'range': False, 'loc': True}))
        except Exception as e:
            pytest.fail('%s: script does not parse: %s' % (page.name, e))
    return trees


def walk(node):
    if isinstance(node, list):
        for n in node:
            yield from walk(n)
    elif hasattr(node, 'type'):
        yield node
        for key, value in vars(node).items():
            if key not in ('loc', 'range'):
                yield from walk(value)


@pytest.mark.parametrize('page', PAGES, ids=lambda p: p.name)
def test_page_scripts_parse(page):
    assert parsed(page) or not page_scripts(page)


# --------------------------------------------------------------------------
# 3. Page scripts do not reuse names the browser owns
# --------------------------------------------------------------------------

# Properties of `window`. A top-level `var x` or `function x` in a page script
# IS window.x — for these names that silently does nothing, or breaks the page,
# and only on some browsers. (The browser test checks against the real list of
# the browser it runs; this one works without a browser.)
BROWSER_NAMES = set('''
    orientation name status top parent self window frames length closed opener
    location origin history event screen external navigator document performance
    crypto caches cookieStore scheduler menubar toolbar locationbar personalbar
    scrollbars statusbar visualViewport speechSynthesis styleMedia customElements
    innerWidth innerHeight outerWidth outerHeight screenX screenY screenLeft
    screenTop pageXOffset pageYOffset scrollX scrollY devicePixelRatio
    isSecureContext crossOriginIsolated localStorage sessionStorage indexedDB
    focus blur open close stop print find alert confirm prompt fetch scroll
    scrollTo scrollBy moveTo moveBy resizeTo resizeBy postMessage getSelection
    getComputedStyle matchMedia requestAnimationFrame cancelAnimationFrame
    setTimeout clearTimeout setInterval clearInterval atob btoa
    queueMicrotask structuredClone reportError createImageBitmap console
    onload onerror onresize onscroll onclick offscreenBuffering clientInformation
    defaultStatus defaultstatus frameElement trustedTypes navigation launchQueue
    sharedStorage documentPictureInPicture credentialless chrome
'''.split())


def top_level_names(page):
    names = {}
    for tree in parsed(page):
        for stmt in tree.body:
            if stmt.type == 'FunctionDeclaration':
                names[stmt.id.name] = stmt.loc.start.line
            elif stmt.type == 'VariableDeclaration':
                for d in stmt.declarations:
                    if d.id.type == 'Identifier':
                        names[d.id.name] = stmt.loc.start.line
    return names


@pytest.mark.parametrize('page', PAGES, ids=lambda p: p.name)
def test_page_scripts_do_not_shadow_browser_names(page):
    clash = sorted(set(top_level_names(page)) & BROWSER_NAMES)
    assert not clash, (
        '%s declares %s at top level; the browser already owns these names '
        '(window.%s). Rename them.' % (page.name, clash, clash[0]))


# --------------------------------------------------------------------------
# 4. Every text key has both languages
# --------------------------------------------------------------------------

def language_tables(page):
    """{'en': {keys}, 'de': {keys}} from the page's i18n object, or None."""
    for tree in parsed(page):
        for node in walk(tree):
            if node.type != 'ObjectExpression':
                continue
            langs = {}
            for prop in node.properties:
                key = getattr(prop.key, 'name', None) or getattr(prop.key, 'value', None)
                if key in ('en', 'de') and prop.value.type == 'ObjectExpression':
                    langs[key] = {getattr(p.key, 'name', None) or getattr(p.key, 'value', None)
                                  for p in prop.value.properties}
            if len(langs) == 2:
                # later additions: i18n.en.key = '...'
                for tree2 in parsed(page):
                    for n in walk(tree2):
                        if (n.type == 'AssignmentExpression'
                                and n.left.type == 'MemberExpression'
                                and n.left.object.type == 'MemberExpression'
                                and getattr(n.left.object.property, 'name', None) in langs
                                and getattr(n.left.property, 'name', None)):
                            langs[n.left.object.property.name].add(n.left.property.name)
                return langs
    return None


def used_keys(page):
    html = page.read_text()
    keys = set(re.findall(r'data-(?:t|i18n)(?:-placeholder|-html)?="([\w.\-]+)"', html))
    for tree in parsed(page):
        for node in walk(tree):
            if (node.type == 'CallExpression' and node.callee.type == 'Identifier'
                    and node.callee.name == 't' and node.arguments
                    and node.arguments[0].type == 'Literal'
                    and isinstance(node.arguments[0].value, str)):
                keys.add(node.arguments[0].value)
    return keys


@pytest.mark.parametrize('page', PAGES, ids=lambda p: p.name)
def test_every_text_has_both_languages(page):
    langs = language_tables(page)
    if langs is None:
        pytest.skip('no translation table in this page')
    problems = []
    for a, b in (('en', 'de'), ('de', 'en')):
        only = sorted(langs[a] - langs[b])
        if only:
            problems.append('in %s but not in %s: %s' % (a, b, only))
    missing = sorted(used_keys(page) - (langs['en'] & langs['de']) - (langs['en'] ^ langs['de']))
    if missing:
        problems.append('used on the page but in no table (shows up raw): %s' % missing)
    assert not problems, '%s:\n  %s' % (page.name, '\n  '.join(problems))


# --------------------------------------------------------------------------
# 5. Pages and the on-device menu only call addresses the server routes
# --------------------------------------------------------------------------

def server_routes():
    src = (FRAME / 'routes.py').read_text()
    get_src = src.split('def do_GET')[1].split('def do_POST')[0]
    post_src = src.split('def do_POST')[1].split('# --- Page serving ---')[0]
    pat_exact = r"path == '(/[^']*)'"
    pat_prefix = r"path\.startswith\('(/[^']*)'\)"
    return {
        'GET': (set(re.findall(pat_exact, get_src)), set(re.findall(pat_prefix, get_src))),
        'POST': (set(re.findall(pat_exact, post_src)), set(re.findall(pat_prefix, post_src))),
    }


def leading_literal(node):
    """The fixed start of a URL expression: 'x' or 'x' + something."""
    while node.type == 'BinaryExpression':
        node = node.left
    if node.type == 'Literal' and isinstance(node.value, str):
        return node.value
    if node.type == 'TemplateLiteral' and node.quasis:
        return node.quasis[0].value.cooked
    return None


def page_calls(page):
    """(method, path, line) for fetch()/postJson()/api() calls in a page."""
    calls = []
    for tree in parsed(page):
        for node in walk(tree):
            if node.type != 'CallExpression' or node.callee.type != 'Identifier':
                continue
            fn = node.callee.name
            if fn not in ('fetch', '_fetch', 'postJson', 'getJson', 'api') or not node.arguments:
                continue
            url = leading_literal(node.arguments[0])
            if not url or not url.startswith('/'):
                continue
            method = 'GET'
            if fn == 'postJson' or (fn == 'api' and len(node.arguments) > 1):
                method = 'POST'
            elif len(node.arguments) > 1 and node.arguments[1].type == 'ObjectExpression':
                for p in node.arguments[1].properties:
                    if getattr(p.key, 'name', None) == 'method' and p.value.type == 'Literal':
                        method = str(p.value.value).upper()
            calls.append((method, url.split('?')[0], node.loc.start.line))
    return calls


def python_calls():
    """(method, path, where) for the on-device menu's calls to the server."""
    calls = []
    for path in PY_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('api_get', 'api_post', 'act') and node.args):
                continue
            arg = node.args[0]
            while isinstance(arg, ast.BinOp):
                arg = arg.left
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.startswith('/'):
                method = 'GET' if node.func.attr == 'api_get' else 'POST'
                calls.append((method, arg.value.split('?')[0],
                              '%s:%d' % (path.relative_to(REPO), node.lineno)))
    return calls


def routed(method, path, routes):
    exact, prefixes = routes[method]
    return path in exact or any(path.startswith(p) for p in prefixes)


def test_pages_and_menu_only_call_routes_that_exist():
    routes = server_routes()
    assert len(routes['GET'][0]) > 20 and len(routes['POST'][0]) > 20
    calls = [(m, p, '%s:%d' % (page.name, line))
             for page in PAGES for m, p, line in page_calls(page)]
    calls += python_calls()
    assert len(calls) > 60, 'call extraction broke (found %d)' % len(calls)
    bad = []
    for method, path, where in calls:
        if not routed(method, path, routes):
            other = 'POST' if method == 'GET' else 'GET'
            hint = ' (exists as %s)' % other if routed(other, path, routes) else ''
            bad.append('%s  %s %s%s' % (where, method, path, hint))
    assert not bad, 'called, but the server has no such route:\n  ' + '\n  '.join(bad)
