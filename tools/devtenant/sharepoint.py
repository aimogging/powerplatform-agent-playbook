"""SharePoint REST for the dev tenant: idempotent list/field provisioning, fixtures, files and folders.

Every behaviour marked MEASURED below was observed on a real tenant; see reference/platform-traps.md.
  * POST/MERGE/DELETE need X-RequestDigest from _api/contextinfo (the connector adds it for flows; raw REST does not).
  * Item responses carry BOTH "Id" and "ID". Python keeps both; PowerShell's ConvertFrom-Json throws on them and
    Invoke-RestMethod then silently hands back a string (a classic "blank IDs" bug) -- not a problem here.
  * CreateFieldAsXml with Options 24 (AddFieldInternalNameHint 8 + AddToDefaultView 16).
  * A field named "Sha256" gets InternalName _x0053_ha256 (a hex-escaped leading letter). Audit by InternalName FIRST,
    then Title; a Title hit with a different InternalName is a mangled name -- never create a twin next to it.
  * addUsingPath creates ONE folder level (missing parent = HTTP 500, which a flow's connector reports as BadGateway);
    an existing folder is 400 unless overwrite=true (200, contents intact).
  * SP.MoveCopyUtil.CopyFolder copies the CONTENTS flat into destUrl; it does not nest a subfolder.
  * Unstorable values answer HTTP 500, not 400: a DateTime before 1900-01-01, single-line text over its MaxLength.
  * URL fields: Url and Description are capped at 255 characters.
"""
import re
import time
import urllib.parse
from xml.sax.saxutils import escape, quoteattr

from .http import HttpError

NOMETA = 'application/json;odata=nometadata'
VERBOSE = 'application/json;odata=verbose'


def q(s):
    """Quote a value inside an _api path literal: apostrophes double."""
    return str(s).replace("'", "''")


def field_xml(f):
    """Field definition (schema JSON) -> SchemaXml for CreateFieldAsXml."""
    name, ftype = f['name'], f['type']
    attrs = [('Name', name), ('StaticName', name), ('DisplayName', f.get('displayName', name)), ('Type', ftype)]
    if f.get('required'):
        attrs.append(('Required', 'TRUE'))
    if f.get('indexed'):
        attrs.append(('Indexed', 'TRUE'))
    inner = ''
    if ftype == 'Text':
        attrs.append(('MaxLength', str(f.get('maxLength', 255))))
    elif ftype == 'Note':
        attrs += [('NumLines', str(f.get('numLines', 6))), ('RichText', 'TRUE' if f.get('richText') else 'FALSE'),
                  ('AppendOnly', 'FALSE')]
    elif ftype == 'Number':
        attrs.append(('Decimals', str(f.get('decimals', 0))))
        if 'min' in f:
            attrs.append(('Min', str(f['min'])))
    elif ftype in ('Choice', 'MultiChoice'):
        attrs.append(('Format', f.get('format', 'Dropdown')))
        inner = '<CHOICES>%s</CHOICES>' % ''.join('<CHOICE>%s</CHOICE>' % escape(c) for c in f.get('choices', []))
        if f.get('default') is not None:
            inner += '<Default>%s</Default>' % escape(str(f['default']))
    elif ftype == 'DateTime':
        attrs.append(('Format', f.get('format', 'DateOnly')))
    elif ftype == 'User':
        attrs.append(('UserSelectionMode', f.get('selectionMode', 'PeopleOnly')))
    elif ftype == 'URL':
        attrs.append(('Format', f.get('format', 'Hyperlink')))
    elif ftype == 'Boolean':
        inner = '<Default>%s</Default>' % ('1' if f.get('default') else '0')
    head = ' '.join('%s=%s' % (k, quoteattr(v)) for k, v in attrs)
    return '<Field %s>%s</Field>' % (head, inner) if inner else '<Field %s />' % head


class SharePoint(object):
    def __init__(self, cfg, auth, http, site_url=None, clock=time.time):
        self.cfg, self.auth, self.http = cfg, auth, http
        self.site = (site_url or cfg.get('siteUrl') or '').rstrip('/')
        self._digest, self._digest_at, self.clock = None, 0, clock

    # ----------------------------------------------------------------------------- transport
    def _headers(self, accept=NOMETA, extra=None):
        h = self.auth.headers('sharepoint', {'Accept': accept})
        h.update(extra or {})
        return h

    def digest(self):
        if self._digest and self.clock() - self._digest_at < 1500:
            return self._digest
        d = self.http.json('POST', self.site + '/_api/contextinfo', headers=self._headers(), body={}, allow_write_retry=True)
        self._digest, self._digest_at = d['FormDigestValue'], self.clock()
        return self._digest

    def get(self, path):
        return self.http.json('GET', self.site + '/_api/' + path, headers=self._headers())

    def post(self, path, body=None, merge=False, verbose=True, raw=None, extra=None):
        h = self._headers(VERBOSE if verbose else NOMETA, {'X-RequestDigest': self.digest()})
        if verbose and raw is None:
            h['Content-Type'] = VERBOSE
        if merge:
            h.update({'X-HTTP-Method': 'MERGE', 'IF-MATCH': '*'})
        h.update(extra or {})
        if raw is not None:
            r = self.http.request('POST', self.site + '/_api/' + path, headers=h, raw_body=raw)
        else:
            r = self.http.request('POST', self.site + '/_api/' + path, headers=h, body=body if body is not None else {})
        if r.status not in (200, 201, 204):
            raise HttpError('POST', path, r.status, r.text)
        return r.json() if r.body else None

    def delete(self, path):
        h = self._headers(NOMETA, {'X-RequestDigest': self.digest(), 'X-HTTP-Method': 'DELETE', 'IF-MATCH': '*'})
        r = self.http.request('POST', self.site + '/_api/' + path, headers=h, body={})
        if r.status not in (200, 204):
            raise HttpError('DELETE', path, r.status, r.text)

    # ----------------------------------------------------------------------------- lists and fields
    def find_list(self, title):
        r = self.get("web/lists?$select=Id,Title,ItemCount,ListItemEntityTypeFullName&$filter=Title eq '%s'&$top=2" % q(title))
        rows = (r or {}).get('value') or []
        return rows[0] if rows else None

    def fields(self, list_id):
        r = self.get("web/lists(guid'%s')/fields?$select=InternalName,Title,TypeAsString,SchemaXml&$filter=Hidden eq false" % list_id)
        return (r or {}).get('value') or []

    def plan_list(self, spec):
        """Audit one list spec against the site. Returns (list_row_or_None, [actions]); nothing is written."""
        actions = []
        row = self.find_list(spec['title'])
        if row is None:
            actions.append(('create-list', spec['title'], None))
            for f in spec.get('fields', []):
                actions.append(('create-field', f['name'], f))
            if spec.get('titleDisplayName'):
                actions.append(('rename-title', spec['titleDisplayName'], None))
            return None, actions
        have = self.fields(row['Id'])
        by_internal = {x['InternalName']: x for x in have}
        by_title = {x['Title'].lower(): x for x in have}
        for f in spec.get('fields', []):
            hit = by_internal.get(f['name'])
            if hit is None:
                t = by_title.get(f.get('displayName', f['name']).lower()) or by_title.get(f['name'].lower())
                if t is not None:
                    actions.append(('mangled', f['name'], 'a field titled %r exists with InternalName %r -- SharePoint '
                                    'rewrote the name; rename the spec field (e.g. ContentHash, not Sha256) instead of '
                                    'creating a twin' % (t['Title'], t['InternalName'])))
                    continue
                actions.append(('create-field', f['name'], f))
                continue
            if f['type'] in ('Choice', 'MultiChoice'):
                existing = [c.replace('&amp;', '&') for c in re.findall(r'<CHOICE>(.*?)</CHOICE>', hit.get('SchemaXml', ''))]
                missing = [c for c in f.get('choices', []) if c not in existing]
                if missing:
                    actions.append(('append-choices', f['name'], existing + missing))
        title_field = by_internal.get('Title')
        if spec.get('titleDisplayName') and title_field and title_field['Title'] != spec['titleDisplayName']:
            actions.append(('rename-title', spec['titleDisplayName'], None))
        return row, actions

    def apply_list(self, spec, actions):
        row = self.find_list(spec['title'])
        for kind, name, arg in actions:
            if kind == 'mangled':
                raise SystemExit('refusing: %s: %s' % (name, arg))
            if kind == 'create-list':
                self.post('web/lists', {'__metadata': {'type': 'SP.List'}, 'BaseTemplate': spec.get('template', 100),
                                        'Title': spec['title'], 'Description': spec.get('description', ''),
                                        'AllowContentTypes': True, 'ContentTypesEnabled': False})
                row = self.find_list(spec['title'])
            elif kind == 'create-field':
                self.post("web/lists(guid'%s')/fields/CreateFieldAsXml" % row['Id'],
                          {'parameters': {'__metadata': {'type': 'SP.XmlSchemaFieldCreationInformation'},
                                          'SchemaXml': field_xml(arg), 'Options': 24}})
            elif kind == 'append-choices':
                ftype = 'SP.FieldMultiChoice' if any(f['name'] == name and f['type'] == 'MultiChoice' for f in spec['fields']) else 'SP.FieldChoice'
                self.post("web/lists(guid'%s')/fields/GetByInternalNameOrTitle('%s')" % (row['Id'], q(name)),
                          {'__metadata': {'type': ftype}, 'Choices': {'__metadata': {'type': 'Collection(Edm.String)'}, 'results': arg}},
                          merge=True)
            elif kind == 'rename-title':
                self.post("web/lists(guid'%s')/fields/GetByInternalNameOrTitle('Title')" % row['Id'],
                          {'__metadata': {'type': 'SP.Field'}, 'Title': name}, merge=True)
        return row

    def recycle_bin(self, title):
        """Recycle-bin entries (both stages) whose Title equals `title` -> [(scope, id)]."""
        hits = []
        for scope in ('web', 'site'):
            r = self.get("%s/RecycleBin?$select=Id,Title,ItemType&$filter=Title eq '%s'" % (scope, q(title)))
            hits.extend((scope, x['Id']) for x in ((r or {}).get('value') or []))
        return hits

    def delete_list(self, title, purge=True):
        """Delete a list the TEST created, then purge it from both recycle-bin stages. Returns what was removed."""
        row = self.find_list(title)
        done = []
        if row:
            self.post("web/lists(guid'%s')/recycle()" % row['Id'], verbose=False)
            done.append('list recycled')
        if purge:
            # deleting from the first-stage bin MOVES the entry to the second stage, and the site-level query lists
            # the first stage too -- so re-query after every pass (live-measured: a stale second delete answers 400)
            for _ in range(4):
                hits = self.recycle_bin(title)
                if not hits:
                    break
                for scope, rid in hits:
                    try:
                        self.post("%s/RecycleBin('%s')/deleteObject()" % (scope, rid), verbose=False)
                        done.append('purged from the %s recycle bin' % scope)
                    except HttpError as ex:
                        if ex.status not in (400, 404):
                            raise
        return done

    # ----------------------------------------------------------------------------- items (fixtures)
    def items(self, title, select='*', filt=None, top=500):
        path = "web/lists/GetByTitle('%s')/items?$select=%s&$top=%d" % (q(title), select, top)
        if filt:
            path += '&$filter=' + urllib.parse.quote(filt, safe="=' ()")
        out = []
        r = self.get(path)
        while r:
            out.extend(r.get('value') or [])
            nxt = r.get('odata.nextLink') or r.get('@odata.nextLink')
            r = self.http.json('GET', nxt, headers=self._headers()) if nxt else None
        return out

    def add_item(self, title, fields):
        lst = self.find_list(title)
        body = {'__metadata': {'type': lst['ListItemEntityTypeFullName']}}   # read, never hardcode SP.Data.*ListItem
        body.update(fields)
        return self.post("web/lists/GetByTitle('%s')/items" % q(title), body)

    def update_item(self, title, item_id, fields):
        lst = self.find_list(title)
        body = {'__metadata': {'type': lst['ListItemEntityTypeFullName']}}
        body.update(fields)
        return self.post("web/lists/GetByTitle('%s')/items(%d)" % (q(title), int(item_id)), body, merge=True)

    def delete_item(self, title, item_id):
        self.delete("web/lists/GetByTitle('%s')/items(%d)" % (q(title), int(item_id)))

    def seed(self, title, rows, tag):
        """Create fixture rows whose Title starts with `tag` so cleanup can find exactly them."""
        made = []
        for r in rows:
            f = dict(r)
            f['Title'] = '%s %s' % (tag, f.get('Title', '')).strip()
            made.append(self.add_item(title, f))
        return made

    def cleanup(self, title, tag):
        """Delete ONLY rows whose Title starts with `tag` (a fixture never touches rows it did not create)."""
        gone = 0
        for it in self.items(title, select='Id,Title', filt="startswith(Title,'%s')" % q(tag)):
            self.delete_item(title, it['Id'])
            gone += 1
        return gone

    # ----------------------------------------------------------------------------- files and folders
    def server_relative(self, library_relative):
        """'Shared Documents/a/b' -> '/sites/<site>/Shared Documents/a/b' (REST wants server-relative; the flow
        connector's folderPath wants library-relative -- keep ONE library-relative root and derive the other)."""
        path = urllib.parse.urlparse(self.site).path.rstrip('/')
        return path + '/' + library_relative.strip('/')

    def ensure_folder(self, library_relative):
        """Create every missing level (addUsingPath is single-level)."""
        parts = library_relative.strip('/').split('/')
        for i in range(2, len(parts) + 1):
            sr = self.server_relative('/'.join(parts[:i]))
            self.post("web/folders/addUsingPath(DecodedUrl='%s',overwrite=true)" % q(sr), verbose=False)
        return self.server_relative(library_relative)

    def upload(self, library_folder, name, data):
        sr = self.server_relative(library_folder)
        return self.post("web/GetFolderByServerRelativePath(decodedurl='%s')/Files/addUsingPath(DecodedUrl='%s',overwrite=true)"
                         % (q(sr), q(name)), raw=data, verbose=False)

    def move_folder(self, src_library_relative, dst_library_relative):
        origin = self.cfg.site_origin
        return self.post('SP.MoveCopyUtil.MoveFolder', {'srcUrl': origin + self.server_relative(src_library_relative),
                                                        'destUrl': origin + self.server_relative(dst_library_relative)},
                         verbose=False)
