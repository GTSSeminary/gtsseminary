#!/usr/bin/env python3
"""GTS lightweight CMS server.

Python stdlib only (http.server + sqlite3 + hmac + hashlib). Serves the
GTS website live from cms/_render/*.html, replacing every [data-content-key]
carrier with the *published* copy stored in SQLite. Design stays untouched
(the annotated copies are byte-identical to the originals except the keys).

Admin lives at /admin: login (PBKDF2 + signed session cookie), a dashboard,
per-page editors grouped by section, draft->publish workflow, and a preview
mode that renders the real page with draft copy.

Run:  python3 cms/gts_cms.py  [port]   (default 8000)
"""
import os
import re
import sys
import json
import hmac
import hashlib
import sqlite3
import secrets
import html as htmllib
import mimetypes
import urllib.parse
import datetime
import email.utils
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from lxml import html as lhtml
from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
RENDER = os.path.join(HERE, '_render')
DB = os.path.join(HERE, 'gts.db')
SCHEMA = json.load(open(os.path.join(HERE, 'schema.json'), encoding='utf-8'))
SEED = json.load(open(os.path.join(HERE, 'seed.json'), encoding='utf-8'))

PAGE_SLUG = {'index': 'home', 'about': 'about', 'academics': 'academics',
             'admission': 'admission', 'events': 'news', 'faq': 'faq',
             'resources': 'resources', 'contact': 'contact'}
PAGES = ['index', 'about', 'academics', 'admission', 'events', 'faq',
         'resources', 'contact']
PAGE_LABEL = {'index': 'Home', 'about': 'About', 'academics': 'Academics',
              'admission': 'Admission', 'events': 'News & Events',
              'faq': 'FAQ', 'resources': 'Resources', 'contact': 'Contact'}
GLOBAL_KEYS = [k for k in SCHEMA if k.startswith('global.')]

SECRET = os.environ.get('GTS_SECRET', 'dev-secret-change-me')
PBKDF2_ITERS = 200_000
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

_page_cache = {}


def db_connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS content_items (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        updated_at TEXT,
        updated_by TEXT
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    ''')
    db.commit()
    return db


def seed_db(db):
    if db.execute('SELECT COUNT(*) FROM content_items').fetchone()[0]:
        return
    db.executemany('INSERT INTO content_items (key, value, status) VALUES (?,?,?)',
                   [(k, v, 'published') for k, v in SEED.items()])
    db.commit()


def ensure_admin(db):
    if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
        return
    pw = hash_password(os.environ.get('GTS_ADMIN_PASSWORD', 'admin'))
    db.execute('INSERT INTO users (username, password_hash) VALUES (?,?)', ('admin', pw))
    db.commit()


def published_values(db):
    vals = {k: v for k, v in SEED.items()}
    for r in db.execute("SELECT key, value FROM content_items WHERE status='published'"):
        vals[r['key']] = r['value']
    return vals


def page_keys(slug):
    prefix = slug + '.'
    return [k for k in SCHEMA if k.startswith(prefix)]


def sanitize(value):
    """Plain text only: strip any markup the client may have sent."""
    value = re.sub(r'<[^>]*>', '', str(value))
    value = value.replace('\u0000', '')
    return re.sub(r'\s+', ' ', value).strip()


def validate(key, value):
    spec = SCHEMA.get(key)
    if not spec:
        return True, ''
    if not value.strip():
        return False, 'is required'
    if len(value) > spec['max_chars']:
        return False, 'exceeds %d characters' % spec['max_chars']
    return True, ''


# ---------------------------------------------------------------------------
# security: password hashing + sessions + csrf
# ---------------------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ITERS)
    return '%s$%s' % (salt, dk.hex())


def verify_password(password, stored):
    try:
        salt, digest = stored.split('$')
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ITERS)
    return hmac.compare_digest(digest, dk.hex())


def new_session(db, username):
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO sessions (token, username) VALUES (?,?)', (token, username))
    db.commit()
    return token


def user_for_session(db, token):
    if not token:
        return None
    r = db.execute('SELECT username FROM sessions WHERE token=?', (token,)).fetchone()
    return r['username'] if r else None


def sign(val):
    return '%s.%s' % (val, hmac.new(SECRET.encode(), val.encode(), hashlib.sha256).hexdigest())


def unsign(signed):
    try:
        val, sig = signed.rsplit('.', 1)
    except (ValueError, AttributeError):
        return None
    expect = hmac.new(SECRET.encode(), val.encode(), hashlib.sha256).hexdigest()
    return val if hmac.compare_digest(sig, expect) else None


def csrf_for(user):
    return hmac.new(SECRET.encode(), user.encode(), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def inject(el, value):
    """Replace the editable text of a stamped element with `value`, treated
    as plain text (never parsed as markup). Design children (svg icons,
    em/span accents) are preserved: if a child's current text still appears
    inside the new value the text is split around it so the styled run keeps
    its markup; otherwise the child is folded into the plain text so no
    duplicate copy is shown. lxml escapes text on serialization, so `value`
    is stored as-is and can never inject markup."""
    children = list(el)
    if not children:
        el.text = value
        return
    text_children = [c for c in children if (c.text or '').strip() and c.tag not in ('svg', 'img')]
    if not text_children:
        icon_children = [c for c in children if c.tag in ('svg', 'img')]
        tailed = [c for c in icon_children if (c.tail or '').strip()]
        if tailed:
            last = tailed[-1]
            for c in children:
                if c is not last:
                    c.tail = None
            last.tail = value
            return
        el.text = value
        for c in children:
            c.tail = None
        return
    child = text_children[0]
    ct = child.text.strip()
    if ct and ct in value:
        before, after = value.split(ct, 1)
        el.text = before or None
        child.text = ct
        child.tail = after or None
        for c in children:
            if c is not child:
                c.tail = None
    else:
        # styled phrase no longer present in the new copy: fold it into the
        # plain text and drop the child so nothing is duplicated
        child.drop_tree()
        el.text = value


def render_html(db, page, draft_vals=None):
    """Render an annotated page with published values (or draft overrides)."""
    path = os.path.join(RENDER, page + '.html')
    tree = lhtml.parse(path)
    vals = published_values(db)
    if draft_vals:
        vals.update(draft_vals)
    targets = [el for el in tree.getroot().iter() if el.get('data-content-key') in vals]
    for el in targets:
        key = el.get('data-content-key')
        inject(el, vals[key])
    return etree.tostring(tree, encoding='unicode', method='html')


def render_public(db, slug):
    global _page_cache
    if slug in _page_cache:
        return _page_cache[slug]
    page = [p for p in PAGES if PAGE_SLUG[p] == slug][0]
    out = render_html(db, page).encode('utf-8')
    _page_cache[slug] = out
    return out


def flush_cache():
    global _page_cache
    _page_cache = {}


# ---------------------------------------------------------------------------
# http server
# ---------------------------------------------------------------------------
def esc(v):
    return htmllib.escape(str(v), quote=True)


def admin_action_form(text, confirm=''):
    return ('<form method="post" class="cms-inline" onsubmit="return confirm(\'%s\');">'
            '<input type="hidden" name="csrf" value="__CSRF__">%s'
            '<button class="cms-btn cms-danger">%s</button></form>'
            % (esc(confirm or 'Are you sure?'), '', esc(text)))


class CMSHandler(BaseHTTPRequestHandler):
    server_version = 'GTS-CMS/1.0'

    # sqlite connections are not thread-safe; open a fresh one per request
    def setup(self):
        super().setup()
        self.db = db_connect()
        seed_db(self.db)
        ensure_admin(self.db)

    # ------------------------------------------------------------- helpers
    def log_message(self, fmt, *args):
        sys.stderr.write('[%s %s] %s\n' % (self.command, self.path, fmt % args))

    def _send(self, data, ctype='text/html; charset=utf-8', status=200, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc, flash=None, cookie=None):
        if flash:
            sep = '&' if '?' in loc else '?'
            loc += sep + 'msg=' + urllib.parse.quote(flash)
        headers = {'Location': loc}
        if cookie:
            headers['Set-Cookie'] = cookie
        self._send(self._redir_body(loc), status=303, headers=headers)

    @staticmethod
    def _redir_body(loc):
        return ('<html><head><meta http-equiv="refresh" content="0;url=%s"></head>'
                '<body><a href="%s">Redirecting…</a></body></html>' % (esc(loc), esc(loc))).encode()

    def _session_token(self):
        for part in self.headers.get('Cookie', '').split(';'):
            part = part.strip()
            if part.startswith('gts='):
                return unsign(part[4:])
        return None

    def _current_user(self):
        tok = self._session_token()
        return user_for_session(self.db, tok) if tok else None

    def _csrf(self):
        user = self._current_user()
        return csrf_for(user) if user else ''

    def _check_csrf(self, form):
        vals = form.get('csrf')
        given = vals[0] if vals else ''
        return hmac.compare_digest(given, self._csrf())

    def _form(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length).decode('utf-8', 'replace') if length else ''
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    @staticmethod
    def _first(form, name):
        vals = form.get(name)
        return vals[0] if vals else ''

    def _admin_shell(self, user, title, body):
        page = admin_layout(title, body, user, self._csrf())
        self._send(page.encode('utf-8'))

    # ------------------------------------------------------------- GET routing
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path

        if path.startswith('/admin/assets/'):
            self._admin_asset(path[len('/admin/assets/'):])
            return

        if path.startswith('/admin/preview/'):
            self._preview(path[len('/admin/preview/'):])
            return

        if path in ('/admin/login', '/admin/login/'):
            self._admin_shell(None, 'Sign in', login_form())
            return

        if path == '/admin/logout':
            token = self._session_token()
            if token:
                self.db.execute('DELETE FROM sessions WHERE token=?', (token,))
                self.db.commit()
            self._redirect('/')
            return

        user = self._current_user()
        if path in ('/admin', '/admin/'):
            if not user:
                self._redirect('/admin/login')
                return
            self._admin_shell(user, 'Dashboard', dashboard(self.db, user, self._csrf(), self._msg()))
            return

        if path.startswith('/admin/edit/'):
            if not user:
                self._redirect('/admin/login')
                return
            self._edit(page_for_path(path))
            return

        # ---- public site
        if path in ('/', '/index.html'):
            self._send(render_public(self.db, 'home'), ctype='text/html; charset=utf-8')
            return
        if path.endswith('.html'):
            page = path.strip('/').removesuffix('.html')
            if page in PAGES:
                self._send(render_public(self.db, PAGE_SLUG[page]), ctype='text/html; charset=utf-8')
                return
        if path == '/assets' or path.startswith('/assets/'):
            self._file(path)
            return
        self._send('404 - page not found'.encode(), status=404)

    # ------------------------------------------------------------- POST routing
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path

        if path in ('/admin/login', '/admin/login/'):
            form = self._form()
            username = self._first(form, 'username').strip()
            password = self._first(form, 'password')
            row = self.db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if row and verify_password(password, row['password_hash']):
                token = new_session(self.db, username)
                exp = datetime.datetime.utcnow() + datetime.timedelta(days=30)
                cookie = ('gts=%s; HttpOnly; Path=/; SameSite=Lax; Expires=%s'
                          % (sign(token), email.utils.formatdate(exp.timestamp(), usegmt=True)))
                self._redirect('/admin', cookie=cookie)
            else:
                self._redirect('/admin/login', 'Invalid username or password')
            return

        user = self._current_user()
        if not user:
            self._redirect('/admin/login')
            return

        if path == '/admin/save':
            self._save(user)
            return
        if path == '/admin/publish-all':
            self._publish_all(user)
            return
        self._send(b'404', status=404)

    # ------------------------------------------------------------- auth cookie
    # ------------------------------------------------------------- admin pages
    def _msg(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return qs.get('msg', [''])[0]

    def _edit(self, page):
        slug = PAGE_SLUG[page]
        user = self._current_user()
        keys = page_keys(slug)
        draft = {r['key']: r['value'] for r in self.db.execute(
            "SELECT key, value FROM content_items WHERE status='draft'")}
        self._admin_shell(user, 'Edit · ' + PAGE_LABEL[page],
                          editor_page(page, slug, keys, draft, self._csrf(), self._msg()))

    def _save(self, user):
        form = self._form()
        if not self._check_csrf(form):
            self._send(b'CSRF check failed', status=403)
            return
        page = self._first(form, 'page')
        if page not in PAGES:
            self._send(b'bad page', status=400)
            return
        slug = PAGE_SLUG[page]
        now = datetime.datetime.utcnow().isoformat(timespec='seconds')
        action = self._first(form, 'action') or 'draft'
        errors = []
        saved = 0
        keys = page_keys(slug)
        if 'globals' in form:
            keys = keys + GLOBAL_KEYS
        for key in keys:
            field = 'key_' + key
            if field not in form:
                continue
            value = sanitize(self._first(form, field))
            ok, msg = validate(key, value)
            if not ok:
                errors.append((key, msg))
                continue
            status = 'published' if action == 'publish' else 'draft'
            self.db.execute('INSERT INTO content_items (key, value, status, updated_at, updated_by) '
                            'VALUES (?,?,?,?,?) '
                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value, '
                            'status=excluded.status, updated_at=excluded.updated_at, '
                            'updated_by=excluded.updated_by',
                            (key, value, status, now, user))
            saved += 1
        self.db.commit()
        if action == 'publish':
            flush_cache()
        if errors:
            self._redirect('/admin/edit/' + page, 'Some fields were not saved: %s'
                           % ', '.join(k for k, _ in errors[:3]))
        elif action == 'publish':
            result = self._deploy()
            if result is None:
                msg = 'Saved and published %d field(s).' % saved
            elif result['ok']:
                msg = ('Saved and published %d field(s). Pushed to GitHub — Vercel will '
                       'redeploy in about a minute.' % saved)
            else:
                msg = ('Saved and published %d field(s), but the deploy failed: %s'
                       % (saved, result['err'][:200]))
            self._redirect('/admin/edit/' + page, msg)
        else:
            self._redirect('/admin/edit/' + page, 'Saved %d draft field(s).' % saved)

    def _deploy(self):
        """Run publish.sh to bake content and push. Returns None when the
        deploy is disabled/not configured, or a dict with ok/err."""
        script = os.path.join(SITE, 'publish.sh')
        if not os.path.exists(script):
            return None
        try:
            import subprocess
            proc = subprocess.run([script, 'Publish CMS content updates'],
                                  cwd=SITE, capture_output=True, text=True, timeout=120)
            out = (proc.stdout + proc.stderr).strip()
            if proc.returncode == 0:
                return {'ok': True, 'err': ''}
            return {'ok': False, 'err': out[-500:] or 'publish.sh exited %d' % proc.returncode}
        except subprocess.TimeoutExpired:
            return {'ok': False, 'err': 'publish.sh timed out after 120s'}
        except Exception as exc:
            return {'ok': False, 'err': str(exc)}

    def _publish_all(self, user):
        form = self._form()
        if not self._check_csrf(form):
            self._send(b'CSRF check failed', status=403)
            return
        self.db.execute("UPDATE content_items SET status='published' WHERE status='draft'")
        self.db.commit()
        flush_cache()
        self._redirect('/admin', 'All draft changes published.')

    # ------------------------------------------------------------- preview
    def _preview(self, page):
        user = self._current_user()
        if not user:
            self._redirect('/admin/login')
            return
        if page not in PAGES:
            self._send(b'404', status=404)
            return
        draft = {r['key']: r['value'] for r in self.db.execute(
            "SELECT key, value FROM content_items WHERE status='draft'")}
        html_bytes = render_html(self.db, page, draft_vals=draft).encode('utf-8')
        injected = html_bytes.replace(
            b'</body>', ('<div class="cms-preview-bar">Previewing draft copy - '
                         '<a href="/admin/edit/%s">back to editor</a></div></body>'
                         % page).encode())
        self._send(injected, ctype='text/html; charset=utf-8')

    # ------------------------------------------------------------- assets/files
    def _admin_asset(self, name):
        if name == 'cms.css':
            self._send(CMS_CSS.encode('utf-8'), ctype='text/css')
            return
        if name == 'cms.js':
            self._send(b'', ctype='application/javascript')
            return
        self._send(b'404', status=404)

    def _file(self, path):
        fp = os.path.join(SITE, path.lstrip('/'))
        if os.path.isfile(fp):
            self._send(open(fp, 'rb').read(),
                       ctype=mimetypes.guess_type(path)[0] or 'application/octet-stream')
            return
        self._send(b'404', status=404)


def page_for_path(path):
    page = path[len('/admin/edit/'):]
    return page if page in PAGES else 'index'


# ---------------------------------------------------------------------------
# admin UI
# ---------------------------------------------------------------------------
def login_form():
    return '''<h1>GTS content manager</h1>
<form method="post" action="/admin/login" class="cms-login">
<label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input type="password" name="password" autocomplete="current-password" required></label>
<button class="cms-btn">Sign in</button>
</form>'''


def dashboard(db, user, csrf, msg):
    flash = '<p class="cms-ok">%s</p>' % esc(msg) if msg else ''
    cards = []
    draft_total = 0
    for page in PAGES:
        slug = PAGE_SLUG[page]
        keys = page_keys(slug)
        draft = db.execute("SELECT COUNT(*) FROM content_items WHERE status='draft' AND "
                           "(key=? OR key LIKE ?)", (slug + '.', slug + '.%')).fetchone()[0]
        draft_total += draft
        print_ = 'Preview' if draft else 'View'
        cards.append('''
<article class="cms-card">
 <h3>%s <span class="cms-count">%d fields</span></h3>
 <p class="cms-meta"><span class="%s">● %s</span></p>
 <div class="cms-row">
   %s
   <a class="cms-btn" href="/admin/edit/%s">Edit</a>
 </div>
</article>''' % (PAGE_LABEL[page], len(keys),
                ('cms-dot cms-dot-modified' if draft else 'cms-dot'),
                ('%d draft change(s)' % draft if draft else 'live'),
                ('<a class="cms-btn" href="/admin/preview/%s">Preview</a>' % page if draft else ''),
                page))
    global_draft = db.execute("SELECT COUNT(*) FROM content_items WHERE status='draft' "
                              "AND key LIKE 'global.%'").fetchone()[0]
    glob_card = '''
<article class="cms-card">
 <h3>Site-wide</h3>
 <p class="cms-meta"><span class="%s">● %s</span></p>
 <div class="cms-row">
   %s
   <a class="cms-btn" href="/admin/edit/index">Edit globals</a>
 </div>
</article>''' % (('cms-dot cms-dot-modified' if global_draft else 'cms-dot'),
                ('%d draft change(s)' % global_draft if global_draft else 'live'),
                ('<a class="cms-btn" href="/admin/preview/index">Preview</a>' if global_draft else ''))
    cards.append(glob_card)
    pub_all = admin_action_form2('Publish all drafts', '/admin/publish-all', csrf)
    return '''<h1>Dashboard</h1>%s
<p class="cms-user">Signed in as <strong>%s</strong> · <a href="/">View site</a> · %s</p>
<div class="cms-grid">%s</div>''' % (flash, esc(user), logout_form(csrf), ''.join(cards)) + pub_all


def admin_action_form2(label, action, csrf):
    return ('<form method="post" action="%s" class="cms-inline" '
            'onsubmit="return confirm(\'Publish every draft field across the site?\');">'
            '<input type="hidden" name="csrf" value="%s">'
            '<button class="cms-btn cms-danger">%s</button></form>' % (action, csrf, label))


def logout_form(csrf):
    return ('<form method="get" action="/admin/logout" class="cms-inline">'
            '<button class="cms-btn cms-danger">Log out</button></form>')


def editor_page(page, slug, keys, draft, csrf, msg):
    flash = '<p class="cms-ok">%s</p>' % esc(msg) if msg else ''
    groups = {}
    for key in keys:
        parts = key.split('.')
        section = parts[1] if len(parts) > 1 else 'page'
        groups.setdefault(section, []).append(key)
    global_section = ''
    gkeys = GLOBAL_KEYS
    if gkeys:
        global_section = section_block('Site-wide chrome', gkeys, draft, csrf, '1')
    blocks = [section_block(title, klist, draft, csrf) for title, klist in groups.items()]
    body = chunks = ''.join(blocks) + global_section
    return ('<h1>%s <span class="cms-count">%d fields</span></h1>%s'
            '<div class="cms-toolbar">'
            '<a class="cms-btn" href="/admin/preview/%s">Preview draft</a>'
            '&nbsp;<a class="cms-btn" href="/">View live site</a></div>'
            '<form method="post" action="/admin/save" class="cms-form">'
            '<input type="hidden" name="csrf" value="%s">'
            '<input type="hidden" name="page" value="%s">'
            '%s'
            '<div class="cms-form-actions">'
            '<button class="cms-btn" name="action" value="draft">Save as draft</button>'
            '&nbsp;<label class="cms-check"><input type="checkbox" name="globals" value="1" checked> '
            'include site-wide fields</label>'
            '&nbsp;<button class="cms-btn cms-danger" name="action" value="publish">'
            'Save &amp; publish</button>'
            '</div></form>'
            % (esc(PAGE_LABEL[page]), len(keys), flash, page, esc(csrf), page, body))


def section_block(title, keys, draft, csrf, global_flag=''):
    rows = []
    for key in keys:
        spec = SCHEMA[key]
        current = draft.get(key)
        if current is None:
            current = SEED.get(key, '')
        is_draft = key in draft
        state = '<span class="cms-dot cms-dot-modified" title="draft"></span>' if is_draft else ''
        widget = ('<textarea name="key_%s" rows="%d" maxlength="%d">%s</textarea>'
                  % (esc(key), textarea_rows(spec['max_chars']), spec['max_chars'], esc(current)))
        rows.append('''<div class="cms-field open">
  <div class="cms-field-head" onclick="this.parentNode.classList.toggle('open')">
    <code>%s</code> %s <span class="cms-limit">≤ %d chars</span>
  </div>
  <div class="cms-field-body">%s</div>
</div>''' % (esc(key), state, spec['max_chars'], widget))
    return ('<section class="cms-section"><h2><a href="#%s">%s</a></h2>%s</section>'
            % (esc(title), esc(title.replace('_', ' ').title()), ''.join(rows)))


def textarea_rows(max_chars):
    if max_chars <= 40:
        return 1
    if max_chars <= 80:
        return 2
    if max_chars <= 200:
        return 3
    return 5


def admin_layout(title, body, user, csrf):
    nav = ('<a href="/admin">Dashboard</a><a href="/">View site</a>'
           '<form method="get" action="/admin/logout" class="cms-inline">'
           '<button class="cms-btn cms-danger cms-link">Log out</button></form>') if user else ''
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · GTS CMS</title><link rel="stylesheet" href="/admin/assets/cms.css">
</head><body class="cms">
<header class="cms-top"><div class="cms-brand">GTS <strong>CMS</strong></div>
<nav class="cms-nav">%s</nav></header>
<main class="cms-main">%s</main>
</body></html>''' % (esc(title), nav, body))


CMS_CSS = '''
:root{--navy:#0f2037;--gold:#c9a227;--ivory:#faf7f0;--line:#e3dccb;--ink:#201a12;}
*{box-sizing:border-box}
body.cms{margin:0;background:var(--ivory);color:var(--ink);
 font:15px/1.55 Georgia,"Times New Roman",serif}
.cms-top{background:var(--navy);color:#fff;display:flex;align-items:center;
 justify-content:space-between;padding:.7rem 1.2rem;position:sticky;top:0;z-index:5}
.cms-brand{font-size:1.15rem}.cms-brand strong{color:var(--gold)}
.cms-nav a,.cms-nav button{margin-left:1rem;color:var(--ivory);text-decoration:none;
 background:none;border:none;cursor:pointer;font:inherit}
.cms-nav a:hover{color:var(--gold)}
.cms-main{max-width:880px;margin:0 auto;padding:1.6rem 1.2rem 4rem}
.cms-main h1{font-size:1.6rem;margin:.2rem 0 .8rem}
.cms-user{color:#6b6048;margin:.4rem 0 1.2rem}
.cms-ok{background:#eef7ea;border:1px solid #bcdcad;color:#2c5a1f;padding:.6rem .9rem;border-radius:6px}
.cms-err{background:#fbeaec;border:1px solid #e8b4ba;color:#8a2330;padding:.6rem .9rem;border-radius:6px}
.cms-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:1rem}
.cms-card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:1rem 1.1rem;
 box-shadow:0 1px 2px rgba(15,32,55,.06)}
.cms-card h3{margin:0 0 .4rem;font-size:1.1rem;display:flex;justify-content:space-between;gap:.5rem}
.cms-count{font-size:.78rem;color:#8a7f63;font-weight:normal;align-self:center}
.cms-meta{margin:0 0 .9rem;font-size:.85rem;color:#6b6048}
.cms-dot{display:inline-block;width:.55rem;height:.55rem;border-radius:50%;
 background:#5aa75a;vertical-align:middle;margin-right:.3rem}
.cms-dot-modified{background:var(--gold)}
.cms-row{display:flex;gap:.5rem;flex-wrap:wrap}
.cms-btn{display:inline-block;background:var(--navy);color:#fff;border:1px solid var(--navy);
 border-radius:6px;padding:.45rem .9rem;font:inherit;cursor:pointer;text-decoration:none;font-size:.9rem}
.cms-btn:hover{background:#1b3a63}
.cms-danger{background:#fff;color:var(--navy);border-color:#c9b97e}
.cms-danger:hover{background:#f3ead0}
.cms-inline{display:inline-block;margin:0}
.cms-toolbar{margin:.4rem 0 1.4rem}
.cms-section{background:#fff;border:1px solid var(--line);border-radius:10px;
 margin-bottom:1.4rem;overflow:hidden}
.cms-section>h2{background:#f4efe2;margin:0;padding:.55rem 1rem;font-size:1rem;border-bottom:1px solid var(--line)}
.cms-section>h2 a{color:var(--navy);text-decoration:none;text-transform:capitalize}
.cms-field{border-bottom:1px solid #f0eadc}
.cms-field:last-child{border-bottom:none}
.cms-field-head{padding:.5rem 1rem;cursor:pointer;user-select:none;display:flex;align-items:center;gap:.5rem}
.cms-field-head code{background:#f4efe2;padding:.15rem .4rem;border-radius:4px;
 font-size:.76rem;color:#3c4a5f;word-break:break-all}
.cms-limit{margin-left:auto;font-size:.72rem;color:#a0977c}
.cms-field-body{display:none;padding:.3rem 1rem 1rem}
.cms-field.open .cms-field-head{color:#8a7f3f}
.cms-field.open .cms-field-body{display:block}
.cms-field-body textarea{width:100%;border:1px solid var(--line);border-radius:6px;
 padding:.5rem .6rem;font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;background:#fdfcf8}
.cms-form-actions{margin-top:.4rem}
.cms-pub{margin-top:1.2rem;padding:.9rem 1rem;background:#fff8ea;border:1px solid #e6d5a1;
 border-radius:10px;display:flex;justify-content:space-between;align-items:center;gap:1rem}
.cms-pub label{font-size:.9rem}
.cms-login{max-width:340px;margin:4rem auto;background:#fff;border:1px solid var(--line);
 border-radius:12px;padding:1.6rem;box-shadow:0 2px 10px rgba(15,32,55,.08)}
.cms-login label{display:block;margin-bottom:.9rem}
.cms-login input{width:100%;padding:.5rem .6rem;margin-top:.25rem;border:1px solid var(--line);
 border-radius:6px;font:inherit}
.cms-login button{width:100%;margin-top:.4rem}
.cms-preview-bar{position:sticky;top:0;z-index:60;background:var(--gold);color:#2c2008;
 text-align:center;padding:.4rem;font:13px/1.4 Georgia,serif}
.cms-preview-bar a{color:var(--navy)}
'''

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    flush_cache()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), CMSHandler)
    print('GTS CMS running at http://127.0.0.1:%d  (admin: /admin, default admin/admin)'
          % PORT, file=sys.stderr)
    server.serve_forever()