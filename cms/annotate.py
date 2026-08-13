#!/usr/bin/env python3
"""cms.annotate — build the GTS content inventory + schema + annotated copies.

The originals under SITE/*.html are NEVER modified. This script walks each
page top-down, stamps a stable `data-content-key` on every editable COPY
carrier (headings, paragraphs, lists, buttons, FAQ items, meta…), and writes:

  * cms/_render/*.html   annotated copies the server renders live
  * cms/schema.json      content inventory (key -> type/required/max/group)
  * cms/seed.json        key -> current copy (DB seeding)

Keys are stable slugs: page.section.field  (e.g. home.hero.title)
Design-bearing inline children (em/span/strong/svg icons) are preserved by the
renderer; the annotated key marks ONLY the editable text of an element.
"""
import os
import re
import json
from lxml import html as lhtml
from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
RENDER = os.path.join(HERE, '_render')

PAGES = ['index', 'about', 'academics', 'admission', 'events', 'faq', 'resources', 'contact']
PAGE_SLUG = {'index': 'home', 'about': 'about', 'academics': 'academics',
             'admission': 'admission', 'events': 'news', 'faq': 'faq',
             'resources': 'resources', 'contact': 'contact'}
PAGE_LABEL = {'index': 'Home', 'about': 'About', 'academics': 'Academics',
              'admission': 'Admission', 'events': 'News & Events', 'faq': 'FAQ',
              'resources': 'Resources', 'contact': 'Contact'}

FACTOR = {'serif': 0.44, 'sans': 0.55}
WPT = 6.2
UI = {'script', 'style', 'svg', 'img', 'input', 'path', 'canvas', 'iframe',
      'picture', 'source', 'br'}
CONTAINERS = {'div', 'section', 'article', 'ul', 'ol', 'details', 'form',
              'figure', 'dl', 'header', 'main'}


def css(el):
    return ' '.join(el.get('class', '').split())


def text(el):
    if el is None:
        return ''
    return re.sub(r'\s+', ' ', el.text_content() or '').strip()


def font_px(cls, tag):
    m = re.search(r'text-(xs|sm|base|lg|xl|2xl|3xl|4xl|5xl|6xl|7xl|8xl)(\b|/|\[)', cls)
    if m:
        return {'xs': 12, 'sm': 14, 'base': 16, 'lg': 18, 'xl': 20, '2xl': 24,
                '3xl': 30, '4xl': 36, '5xl': 48, '6xl': 60, '7xl': 72, '8xl': 96}[m.group(1)]
    m2 = re.search(r'text-\[(\d+(?:\.\d+)?)(px|rem|em)\]', cls)
    if m2:
        v, u = m2.groups()
        return float(v) if u == 'px' else float(v) * 16
    return {'h1': 48, 'h2': 36, 'h3': 30, 'h4': 24, 'p': 16, 'a': 16, 'li': 16,
            'span': 16, 'blockquote': 18, 'figcaption': 12, 'summary': 16,
            'strong': 16, 'em': 16, 'small': 14, 'label': 14}.get(tag, 16)


def max_width(cls, tag):
    m = re.search(r'max-w-(xs|sm|md|lg|xl|2xl|3xl|4xl|5xl|6xl|7xl)', cls)
    if m:
        return {'xs': 20, 'sm': 24, 'md': 28, 'lg': 32, 'xl': 36, '2xl': 42,
                '3xl': 48, '4xl': 56, '5xl': 64, '6xl': 72, '7xl': 80}[m.group(1)] * 16
    m2 = re.search(r'max-w-\[(\d+(?:\.\d+)?)px\]', cls)
    if m2:
        return float(m2.group(1))
    return {'h1': 680, 'h2': 680, 'h3': 540, 'h4': 480, 'p': 720, 'a': 420,
            'li': 620, 'span': 420, 'blockquote': 640, 'figcaption': 640,
            'summary': 640, 'strong': 420, 'em': 480, 'small': 420, 'label': 480}.get(tag, 640)


def limits(el, tag):
    cls = css(el)
    fp = font_px(cls, tag)
    w = max_width(cls, tag)
    factor = FACTOR['serif'] if ('serif' in cls or tag in ('h1', 'h2', 'h3', 'h4')) else FACTOR['sans']
    chars_per_line = max(6, int(w * 0.92 / (fp * factor)))
    # web copy wraps over several lines; budget generously so current copy
    # always fits, with headroom for edits
    cur = len(text(el))
    max_chars = max(chars_per_line * 3, int(cur * 1.5) + 10)
    max_words = max(5, int(max_chars / WPT))
    return max_chars, max_words


SCHEMA = {}


def add_key(key, el, ftype, required, group, label):
    if key in SCHEMA:
        return
    tag = el.tag
    mc, mw = limits(el, tag)
    SCHEMA[key] = {'type': ftype, 'required': required, 'max_chars': mc,
                   'max_words': mw, 'group': group, 'label': label or tag}


SEED = {}


def stamp(a, group, el, ftype='text', field=None, num=False):
    """Assign a stable, unique key to `el` and record it.

    `group` is the tuple of name segments (e.g. ('hero',) or
    ('choose_your_path', 'certificate')). Repeated fields (paragraph/bullet/
    question/...) get a sequence number within the group:
      home.choose_your_path.certificate.bullet.2
    so repeats never collide. Singletons (headings, hero title) stay clean
    unless the same base key already exists, in which case a number is added
    (e.g. two `heading` keys in one group become ...heading / ...heading.2)."""
    if el is None or el.get('data-content-key'):
        return
    segs = [a.slug] + list(group)
    if field:
        segs.append(field)
    base = tuple(segs)
    if num:
        seq = a.counts.get(base, 0) + 1
        a.counts[base] = seq
        segs.append(str(seq))
    else:
        key = '.'.join(segs)
        if key in SCHEMA:
            seq = a.counts.get(base, 1) + 1
            a.counts[base] = seq
            segs.append(str(seq))
    key = '.'.join(segs)
    el.set('data-content-key', key)
    add_key(key, el, ftype, required=True, group=a.label, label=None)
    SEED[key] = (el.get('href') or '') if ftype == 'url' else text(el)


HEADING_TAGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
REPEATED_FIELDS = ('paragraph', 'bullet', 'quote', 'accent', 'question', 'meta', 'attribution', 'link')


def walk(a, root, group, first_seen=False):
    """Stamp every editable leaf under `root`. A heading opens a sub-group
    named after its text; its following siblings belong to that sub-group,
    which keeps card bodies keyed under the card title. Recurses into
    containers. `first_seen` tracks whether the page hero group is set.

    A heading visibly promoted to the top of a section (h1/h2) lifts the
    group so *sibling* sections inherit the last section name; card-level
    h3/h4 headings stay inside their own subtree only."""
    cur = group
    promote = False
    pending = []          # deferred kicker paragraphs awaiting their heading
    for el in root.iterchildren():
        tag = el.tag if isinstance(el.tag, str) else None
        if not tag or tag in UI or el.get('data-skip') == '1':
            continue
        if el.get('aria-hidden') == 'true' or 'sr-only' in css(el):
            continue
        if el.get('data-content-key'):
            continue
        cls = css(el)
        is_kicker = tag == 'p' and ('text-gold' in cls or 'text-amber' in cls) and \
            ('text-sm' in cls or 'text-xs' in cls or 'uppercase' in cls)

        if tag in HEADING_TAGS:
            slug = slugify(text(el)) or 'section'
            if not a.found_hero and tag == 'h1':
                cur = ('hero',)
                a.found_hero = True
                promote = True
            elif not a.found_hero:
                cur = (slug,)
                a.found_hero = True
                promote = True
            elif tag == 'h2':
                cur = (slug,)
                promote = True
            else:
                cur = tuple(list(group) + [slug])
            for k in pending:
                stamp(a, cur, k, field='kicker')
            pending = []
            stamp(a, cur, el, field='title' if tag == 'h1' else 'heading')
            continue

        if is_kicker:
            pending.append(el)
            if tag in CONTAINERS or tag in ('p', 'blockquote', 'details'):
                sub_carry, sub_promote = walk(a, el, cur, first_seen)
                if sub_promote:
                    cur = sub_carry
            continue

        if tag == 'p':
            stamp(a, cur, el, field='paragraph', num=True)
        elif tag == 'li':
            stamp(a, cur, el, field='bullet', num=True)
        elif tag == 'blockquote':
            stamp(a, cur, el, field='quote', num=True)
        elif tag == 'summary':
            stamp(a, cur, el, field='question', num=True)
        elif tag == 'figcaption':
            stamp(a, cur, el, field='attribution', num=True)
        elif tag == 'small':
            stamp(a, cur, el, field='meta', num=True)
        elif tag == 'a':
            inner_text = any(c.tag in HEADING_TAGS or c.tag == 'p' for c in el.iterchildren())
            if not inner_text and (text(el) or el.get('href')):
                stamp(a, cur, el, field='link')
        elif tag in ('span', 'strong', 'em') and text(el) and not _has_text_child(el):
            stamp(a, cur, el, field='accent', num=True)

        if tag in CONTAINERS or tag == 'details':
            sub_carry, sub_promote = walk(a, el, cur, first_seen)
            if sub_promote:
                cur = sub_carry
    for k in pending:
        stamp(a, cur, k, field='kicker')
    return cur, promote


def make_annotator(name):
    raw = open(os.path.join(SITE, name + '.html'), encoding='utf-8').read()
    tree = lhtml.fromstring(raw)
    return name, tree


def serialize(tree):
    return etree.tostring(tree, encoding='unicode', method='html')


def slugify(s):
    s = re.sub(r"[^\w]+", "_", s.strip().lower()).strip('_')
    return s[:40] or 'untitled'


def _has_text_child(el):
    return any(c.tag in ('span', 'strong', 'em') and text(c) for c in el.iterchildren())


# ---------------------------------------------------------------------------
# global chrome pass: curated editable copy in the site-wide header/footer
# ---------------------------------------------------------------------------
import copy as _copy


def stamp_global(a, tree):
    """Stamp deterministic `global.*` keys on the shared chrome (topbar,
    header brand, footer). Uses explicit finders so navigation links and
    social icons stay design-controlled."""
    def g(find, key, ftype='text'):
        el = tree.xpath(find)
        if not el:
            return
        el = el[0]
        if el.get('data-content-key'):
            return
        el.set('data-content-key', key)
        add_key(key, el, ftype, required=True, group='Global', label=None)
        SEED[key] = (el.get('href') or '') if ftype == 'url' else text(el)

    tb = "//body/div[contains(@class,'bg-navy-950')]"
    g(tb + "//a[starts-with(@href,'tel:')]", 'global.topbar.phone')
    g(tb + "//a[starts-with(@href,'mailto:')]", 'global.topbar.email')
    g(tb + "//a[contains(@class,'bg-gold')]", 'global.topbar.cta')
    g("//header//span[contains(@class,'font-serif')]", 'global.brand.name')
    g("//header//span[contains(@class,'uppercase') and contains(@class,'text-[')]", 'global.brand.tagline')
    g("//footer//div[contains(@class,'leading-tight')]//span[contains(@class,'font-serif')]", 'global.footer.name')
    g("//footer//div[contains(@class,'leading-tight')]//span[contains(@class,'uppercase')]", 'global.footer.tagline')
    g("//footer//p[contains(.,'A Reformed-Baptist seminary')]", 'global.footer.about')
    g("//footer//a[starts-with(@href,'tel:')]", 'global.footer.phone')
    g("//footer//a[starts-with(@href,'mailto:')]", 'global.footer.email')
    g("//footer//p[contains(.,'Kings Avenue')]", 'global.footer.address')
    g("//footer//p[contains(.,'news about GTS')]", 'global.footer.newsletter')
    g("//footer//p[contains(.,'©')]", 'global.footer.copyright')


def stamp_marquee(a, tree):
    """The home hero marquee is aria-hidden (screen-reader decor) and its
    content is duplicated twice for the seamless scroll loop. Stamp matching
    spans in BOTH copies with the same key so one edit keeps them in sync."""
    copies = tree.xpath("//body/div[contains(@class,'marquee')]//*[contains(@class,'marquee-track')]/*[contains(@class,'flex')]")
    if not copies:
        return
    first = copies[0]
    for seq, span in enumerate(first.xpath(".//span[normalize-space(text())]"), start=1):
        key = '%s.marquee.item.%d' % (a.slug, seq)
        for copy in copies:
            spans = copy.xpath(".//span[normalize-space(text())]")
            if seq - 1 < len(spans):
                el = spans[seq - 1]
                el.set('data-content-key', key)
                if key not in SCHEMA:
                    add_key(key, el, 'text', required=True, group=a.label, label=None)
                    SEED[key] = text(el)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def run():
    os.makedirs(RENDER, exist_ok=True)
    for name in PAGES:
        # annotator context
        class A:
            pass
        a = A()
        a.name = name
        a.slug = PAGE_SLUG[name]
        a.label = PAGE_LABEL[name]
        a.counts = {}
        a.found_hero = False
        raw = open(os.path.join(SITE, name + '.html'), encoding='utf-8').read()
        a.tree = lhtml.fromstring(raw)
        body = a.tree.xpath('//body')[0]

        # strip the shared chrome from stamping (it gets global keys in a
        # separate pass handled by the server config); stamp the page body
        for bad in a.tree.xpath('//header | //footer | //nav | //*[@id="mobileMenu"]'):
            bad.set('data-skip', '1')
        # body-level chrome that duplicates the header/topbar (phone, email,
        # apply CTA, mobile menu) — handled via global.* keys by the server
        xp = ('//body/div[contains(concat(" ", normalize-space(@class), " "), " bg-navy-950 ") and contains(@class, "text-sm")]'
              " | //body/div[contains(@class, 'lg:hidden') and contains(@class, 'bg-white')]"
              " | //body/div[contains(@class,'lg:hidden') and contains(@class,'bg-navy-900')]")
        for bad in a.tree.xpath(xp):
            bad.set('data-skip', '1')

        walk(a, body, ())
        stamp_global(a, a.tree)
        stamp_marquee(a, a.tree)

        with open(os.path.join(RENDER, name + '.html'), 'w', encoding='utf-8') as f:
            f.write(serialize(a.tree))
        n = len([k for k in SCHEMA if k.startswith(PAGE_SLUG[name] + '.')])
        print('%-12s %4d keys' % (name, n))

    with open(os.path.join(HERE, 'schema.json'), 'w', encoding='utf-8') as f:
        json.dump(SCHEMA, f, indent=1, ensure_ascii=False)
    with open(os.path.join(HERE, 'seed.json'), 'w', encoding='utf-8') as f:
        json.dump(SEED, f, indent=1, ensure_ascii=False)
    print('TOTAL %d content keys' % len(SCHEMA))


if __name__ == '__main__':
    run()