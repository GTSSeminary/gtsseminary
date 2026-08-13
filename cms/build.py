#!/usr/bin/env python3
"""cms.build — bake published CMS content into the static site for deployment.

The CMS server injects published values from cms/gts.db into the annotated
copies under cms/_render/ on the fly. Static hosts (Vercel, Netlify, GitHub
Pages) cannot run that server, so this script renders each page exactly as the
live CMS would, strips the CMS marker attributes, and writes the result back
over the originals at SITE/*.html. Push the repo and the edits go live.

Run:  python3 cms/build.py
"""
import os
import sys
import datetime

from lxml import html as lhtml
from lxml import etree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gts_cms

SITE = gts_cms.SITE
RENDER = gts_cms.RENDER
PAGES = gts_cms.PAGES


def doctype_for(name):
    """Keep each page's original doctype (lxml normalizes it otherwise)."""
    head = open(os.path.join(SITE, name + '.html'), encoding='utf-8').read(512)
    return head.split('\n', 1)[0] if head.startswith('<!DOCTYPE') else ''


DOCTYPES = {p: doctype_for(p) for p in PAGES}


def bake(db, name):
    path = os.path.join(RENDER, name + '.html')
    tree = lhtml.parse(path)
    root = tree.getroot()
    vals = gts_cms.published_values(db)
    targets = [el for el in root.iter() if el.get('data-content-key') in vals]
    for el in targets:
        gts_cms.inject(el, vals[el.get('data-content-key')])
    for el in root.iter():
        el.attrib.pop('data-content-key', None)
        el.attrib.pop('data-skip', None)
    body = etree.tostring(root, encoding='unicode', method='html')
    if DOCTYPES.get(name):
        body = DOCTYPES[name] + '\n' + body
    out_path = os.path.join(SITE, name + '.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(body)
    return len(body)


def main():
    if not os.path.exists(gts_cms.DB):
        print('No database found at %s — run the CMS once to seed it.' % gts_cms.DB)
        sys.exit(1)
    db = gts_cms.db_connect()
    for name in PAGES:
        size = bake(db, name)
        print('baked %-12s %7d bytes' % (name, size))
    print('Done. Review with `git diff --stat`, then commit and push.')


if __name__ == '__main__':
    main()
