"""Measure, don't infer: a READ-ONLY census of SharePoint lists before sizing any migration/backfill/cleanup plan.

Lesson: plans sized from run logs or reports that happened to be lying around were wrong -- those artifacts were
weeks stale, counted what THEIR tool needed, and a paged read that stopped short gave a confident wrong total. So:
  * one GET-only choke point (a write here is a bug; the guard refuses it before it leaves the process);
  * per list, the service's ItemCount vs the number actually enumerated (a mismatch = an incomplete read);
  * quality, not just totals: blank required fields, duplicate keys, values that do not parse;
  * a machine-readable JSON result the operator can hand back.
"""
import json
import re


class ReadOnly(object):
    """Wraps devtenant.sharepoint.SharePoint and refuses anything but GET."""

    def __init__(self, sp):
        self._sp = sp

    def get(self, path):
        return self._sp.get(path)

    def items(self, *a, **kw):
        return self._sp.items(*a, **kw)

    def find_list(self, title):
        return self._sp.find_list(title)

    def __getattr__(self, name):
        if name in ('post', 'delete', 'add_item', 'update_item', 'delete_item', 'seed', 'cleanup', 'apply_list',
                    'ensure_folder', 'upload', 'move_folder'):
            raise PermissionError('census is read-only: %s refused' % name)
        return getattr(self._sp, name)


def census(sp, specs):
    """specs: [{"list": title, "required": [field,...], "key": field or null, "patterns": {field: regex}}]."""
    ro = ReadOnly(sp)
    out = []
    for s in specs:
        lst = ro.find_list(s['list'])
        if lst is None:
            out.append({'list': s['list'], 'error': 'not found'})
            continue
        fields = sorted(set(['Id', 'Title'] + s.get('required', []) + ([s['key']] if s.get('key') else []) + list((s.get('patterns') or {}).keys())))
        rows = ro.items(s['list'], select=','.join(fields), top=5000)
        res = {'list': s['list'], 'itemCount': lst.get('ItemCount'), 'enumerated': len(rows)}
        res['completeRead'] = res['itemCount'] == res['enumerated']
        res['blank'] = {f: sum(1 for r in rows if r.get(f) in (None, '')) for f in s.get('required', [])}
        if s.get('key'):
            seen, dups = {}, 0
            for r in rows:
                k = r.get(s['key'])
                if k in (None, ''):
                    continue
                dups += 1 if k in seen else 0
                seen[k] = True
            res['duplicateKeys'] = dups
        res['unparseable'] = {f: sum(1 for r in rows if r.get(f) not in (None, '') and not re.match(p, str(r.get(f))))
                              for f, p in (s.get('patterns') or {}).items()}
        out.append(res)
    return out


def dumps(result):
    return json.dumps(result, indent=1)
