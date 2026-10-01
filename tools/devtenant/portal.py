"""Drive the MAKER PORTALS the way the person receiving the handoff does (Playwright, signed-in persistent profile).

Used by example/run_e2e.py's handoff-import stages to prove the DELIVERABLES -- the .msapp a person opens in Power Apps
Studio and the legacy flow package a person imports in Power Automate -- not just the API deploy used for testing.

Measured on a commercial validation tenant (selectors are the portals' own; they change with portal releases -- every
step screenshots on failure so a broken selector is quick to repair):
  * Power Apps: Apps -> Import app -> "From file (.msapp)" opens a file chooser; Studio then loads the document inside
    the frame named 'EmbeddedStudio' as an unsaved app (URL action=app-from-file). A first-run "Welcome" dialog has Skip.
  * Studio's notification toasts ("Publish successful" for every app ever published) stack over the command bar and
    swallow clicks -- hide them (hide_toasts) before using the Save menu.
  * Save menu = the "More options" button right of Save -> "Save as" -> name box + "Replace existing" picker
    (rows sorted by modified date) -> Save / Replace -> an alertdialog "Replace <app>?" -> Replace.
  * An app another Studio session edited within ~15 minutes is READ-ONLY for this session (lease, C-43): the Replace
    silently does not save. Close Studio with its Back button (releases the lease) and wait before replacing.
  * Publish (Ctrl+Shift+P) -> dialog -> "Publish this version".
  * Power Automate: My flows -> Import -> "Import Package (Legacy)" -> the page is an iframe named 'widgetIFrame' with a
    file input; resources listed with "Create as new"/"Update" and per connection "Select during import"; a slide-in
    pane holds the setup select, the resource name box and the connection list (cells 'div.table-list-cell').
"""
import os
import time

# NOTE: waits use page.wait_for_timeout, never time.sleep: the sync API only processes browser events (new frames,
# navigations) inside Playwright calls, so a time.sleep loop polling page.frame() never sees the frame appear.

HIDE_TOASTS = """() => { let n = 0; document.querySelectorAll('[class*="otificationCard"]').forEach(e => {
  (e.closest('[role=alert],[role=status]') || e.parentElement.parentElement.parentElement).style.display = 'none'; n++; });
  return n; }"""


class PortalError(Exception):
    pass


def hide_toasts(page):
    for fr in page.frames:
        try:
            fr.evaluate(HIDE_TOASTS)
        except Exception:
            pass


def shot(page, out_dir, name):
    try:
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, name + '.png')
        page.screenshot(path=p)
        return p
    except Exception:
        return ''


def studio(page, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        fr = page.frame(name='EmbeddedStudio')
        if fr is not None:
            return fr
        page.wait_for_timeout(1 * 1000)
    raise PortalError('Power Apps Studio did not load (no EmbeddedStudio frame)')


# ------------------------------------------------------------------------------------------- Power Apps Studio
def open_msapp(page, cfg, msapp, first_control, timeout=300):
    """make.powerapps.com -> Apps -> Import app -> From file (.msapp). Returns the Studio frame once `first_control`
    is in the tree view."""
    page.goto('%s/environments/%s/apps' % (cfg.hosts['maker'].rstrip('/'), cfg['environmentId']), wait_until='domcontentloaded')
    page.get_by_text('Import app').first.wait_for(timeout=60000)
    hide_toasts(page)
    page.get_by_text('Import app').first.click()
    with page.expect_file_chooser(timeout=20000) as fc:
        page.get_by_text('From file (.msapp)').click()
    fc.value.set_files(msapp)
    t0 = time.time()
    st = None
    while time.time() - t0 < timeout:
        st = page.frame(name='EmbeddedStudio')
        if st is not None and st.locator('button[aria-label="App checker"]').count() and st.get_by_text(first_control, exact=True).count():
            break
        page.wait_for_timeout(3 * 1000)
    else:
        raise PortalError('Studio did not show %s within %ds' % (first_control, timeout))
    page.wait_for_timeout(5 * 1000)
    try:
        st.get_by_role('button', name='Skip').click(timeout=3000)
    except Exception:
        pass
    hide_toasts(page)
    return st


def formula_errors(page, st):
    """App checker -> Formulas. Returns 0 when Studio says 'No errors found', else the number of rows listed."""
    hide_toasts(page)
    st.locator('button[aria-label="App checker"]').click()
    page.wait_for_timeout(6 * 1000)
    st.get_by_text('Formulas', exact=True).first.click()
    page.wait_for_timeout(3 * 1000)
    if st.get_by_text('No errors found').count():
        return 0
    rows = st.locator('[role=listitem], [role=treeitem]')
    return max(1, rows.count())


def control_text(st, name):
    return (st.locator('[data-control-name="%s"]' % name).first.inner_text() or '').strip()


def _save_menu(page, st):
    hide_toasts(page)
    for b in st.locator('button[aria-label="More options"]').all():
        box = b.bounding_box() or {}
        if box.get('x', 0) > 1000 and box.get('y', 999) < 100:      # the one right of Save in the command bar
            b.click()
            break
    else:
        raise PortalError('Save menu (More options next to Save) not found')
    page.wait_for_timeout(1 * 1000)
    st.get_by_role('menuitem', name='Save as').click(timeout=15000)
    page.wait_for_timeout(2 * 1000)
    return st.get_by_role('dialog').filter(has_text='Save as')


def save_as_new(page, st, name):
    dlg = _save_menu(page, st)
    dlg.get_by_role('textbox').first.fill(name)
    dlg.get_by_role('button', name='Save', exact=True).click()
    return _wait_saved(page, st)


def save_as_replace(page, st, existing_name):
    dlg = _save_menu(page, st)
    dlg.get_by_text('Replace existing').first.click()
    page.wait_for_timeout(2 * 1000)
    rows = dlg.get_by_text(existing_name, exact=True)
    picked = False
    for i in range(rows.count()):
        r = rows.nth(i)
        try:
            if r.is_visible() and (r.bounding_box() or {}).get('y', 0) > 0:
                r.click()
                picked = True
                break
        except Exception:
            continue
    if not picked:
        raise PortalError('Replace existing: no app named %r in the list' % existing_name)
    page.wait_for_timeout(1 * 1000)
    selected = ' '.join(s.inner_text() for s in dlg.locator('[aria-selected="true"], [aria-checked="true"]').all())
    if existing_name not in selected:
        raise PortalError('Replace existing: the selected row is not %r (refusing to overwrite another app)' % existing_name)
    dlg.get_by_role('button', name='Replace', exact=True).click()
    st.get_by_role('alertdialog').get_by_role('button', name='Replace', exact=True).click(timeout=20000)
    return _wait_saved(page, st)


def _wait_saved(page, st, timeout=120):
    """Returns (app_id, read_only). MEASURED: after Save as > Replace existing from a file-opened session the save lands
    as the app's new DRAFT and Studio reloads the app READ-ONLY (the save still holds the editing lease), so Publish is
    not available in that session -- close it (Back -> Leave), wait for the lease (~1 min) and publish from an edit
    session (open_edit)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        url = page.url
        if 'app-id=' in url:
            page.wait_for_timeout(5 * 1000)
            ro = bool(st.get_by_text('Read-only', exact=True).count())
            return url.split('app-id=')[1].split('&')[0].replace('%2F', '/').split('/')[-1], ro
        if st.get_by_text('Read-only', exact=True).count():
            return None, True        # Replace: the URL stays action=app-from-file; the caller verifies the draft by API
        page.wait_for_timeout(2 * 1000)
    raise PortalError('Save did not finish within %ds' % timeout)


def open_edit(page, cfg, app_id, first_control, timeout=300):
    """Open an existing app in Studio for EDIT (what Apps -> ... -> Edit does)."""
    page.goto('%s/e/%s/canvas/?action=edit&app-id=%%2Fproviders%%2FMicrosoft.PowerApps%%2Fapps%%2F%s'
              % (cfg.hosts['maker'].rstrip('/'), cfg['environmentId'], app_id), wait_until='domcontentloaded')
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = page.frame(name='EmbeddedStudio')
        if st is not None and st.locator('button[aria-label="App checker"]').count() and st.get_by_text(first_control, exact=True).count():
            page.wait_for_timeout(5 * 1000)
            try:
                st.get_by_role('button', name='Skip').click(timeout=3000)
            except Exception:
                pass
            hide_toasts(page)
            if st.get_by_text('Read-only', exact=True).count():
                raise PortalError('the app opened READ-ONLY (editing lease held by another session, C-43)')
            return st
        page.wait_for_timeout(3 * 1000)
    raise PortalError('Studio did not open %s for edit within %ds' % (app_id, timeout))


def publish(page, st):
    hide_toasts(page)
    st.locator('button[aria-label="Publish (Ctrl+Shift+P)"]').click()
    st.get_by_role('dialog').get_by_role('button', name='Publish this version').click(timeout=20000)
    page.wait_for_timeout(20 * 1000)


def close_studio(page, st):
    """Back = Studio's own close; it releases the editing lease (navigating away does not, C-43)."""
    try:
        hide_toasts(page)
        st.locator('button[aria-label="Back"]').first.click(timeout=10000)
        page.wait_for_timeout(3 * 1000)
        d = st.locator('[role=dialog],[role=alertdialog]').filter(has_text='Leave the app')
        if d.count():
            d.first.locator('button', has_text='Leave').first.click(timeout=10000)
        page.wait_for_timeout(5 * 1000)
    except Exception:
        pass


# ------------------------------------------------------------------------------------------- Power Automate
def import_flow_package(page, cfg, zip_path, new_name, timeout=600):
    """My flows -> Import -> Import Package (Legacy): upload, Create as new under `new_name`, pick the first existing
    connection for every slot, Import. Returns the final message text."""
    env = cfg['environmentId']
    # = My flows -> Import -> Import Package (Legacy); the direct URL avoids a slow flow-list render
    page.goto('https://make.powerautomate.com/environments/%s/flows/import' % env, wait_until='domcontentloaded')
    t0 = time.time()
    wf = None
    while time.time() - t0 < 300:
        wf = page.frame(name='widgetIFrame')
        if wf is not None and wf.locator('input[type=file]').count():
            break
        page.wait_for_timeout(1 * 1000)
    else:
        raise PortalError('Import package page did not load')
    wf.locator('input[type=file]').first.set_input_files(zip_path)
    wf.get_by_text('Review Package Content').wait_for(timeout=120000)
    hide_toasts(page)
    # the flow row: Create as new + a clear name
    wf.get_by_text('Create as new', exact=True).first.click()
    pane = wf.locator('pa-slide-in-pane')
    pane.locator('select').first.select_option(label='Create as new')
    pane.locator('input[ng-model="$ctrl.resourceName"]').fill(new_name)
    pane.get_by_role('button', name='Save').click()
    page.wait_for_timeout(2 * 1000)
    # every connection slot
    for _ in range(10):
        pending = []
        for row in wf.get_by_role('row').all():
            try:
                txt = row.inner_text()
            except Exception:
                continue
            if 'Select during import' in txt and '@' not in txt:      # a picked slot shows the connection's account
                pending.append(row.get_by_text('Select during import', exact=True).first)
        if not pending:
            break
        pending[0].click()
        page.wait_for_timeout(4 * 1000)
        cells = [c for c in pane.locator('div.table-list-cell').filter(has_text='@').all() if c.is_visible()]
        if not cells:
            raise PortalError('no existing connection to pick in the import pane (create one in the portal first)')
        cells[0].click()
        page.wait_for_timeout(1 * 1000)
        pane.get_by_role('button', name='Save').click(timeout=15000)
        page.wait_for_timeout(3 * 1000)
    btn = wf.get_by_role('button', name='Import', exact=True)
    if not btn.is_enabled():
        raise PortalError('Import button disabled -- a resource is still unresolved')
    btn.click()
    t0 = time.time()
    while time.time() - t0 < timeout:
        body = wf.locator('body').inner_text()
        if 'All package resources were successfully imported' in body:
            return 'All package resources were successfully imported'
        head = body[:1500].lower()
        if 'import failed' in head or 'could not be imported' in head:
            raise PortalError('import failed: %s' % body[:600])
        page.wait_for_timeout(5 * 1000)
    raise PortalError('the import stayed at "Importing your package" for %ds (P-10: asset folder != flow resource key?)' % timeout)


def turn_on_flow(page, cfg, flow_id):
    """Flow details page -> Turn on (an imported flow arrives turned off)."""
    page.goto('https://make.powerautomate.com/environments/%s/flows/%s/details' % (cfg['environmentId'], flow_id),
              wait_until='domcontentloaded')
    t0 = time.time()
    while time.time() - t0 < 180:
        for role in ('menuitem', 'button'):
            btn = page.get_by_role(role, name='Turn on')
            if btn.count():
                hide_toasts(page)
                btn.first.click()
                page.wait_for_timeout(8 * 1000)
                return
        page.wait_for_timeout(2 * 1000)
    raise PortalError('no "Turn on" command on the flow details page')
