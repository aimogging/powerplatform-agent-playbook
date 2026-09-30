"""Drive a PUBLISHED canvas app in a real browser (Playwright) for end-to-end checks and screenshots.

Proven mechanics (a validation tenant):
  * a PERSISTENT browser profile that has signed in once (launch_persistent_context); only ONE process may hold a
    profile at a time; keep the profile outside the repo (it holds a live session);
  * the first launch of an app shows a consent dialog inside an iframe named consentService-iFrame* -> click Allow;
  * the app renders inside the frame named 'fullscreen-app-host'; every control is addressable by its pa.yaml name
    through [data-control-name="<name>"];
  * text inputs: fill the input/textarea INSIDE the control, then Tab so OnChange fires;
  * the compiled runtime package arrives as responses whose URL contains '/appruntime/' -- capture them to run
    devtenant.runtime_rules over what the player actually executes (block service workers or they hide the fetches);
  * after a publish the player may say "You're using an old version" until refreshed -- test only after that is gone;
  * screenshots: park the mouse off the app first so no hover state or tooltip is captured.
Requires: pip install playwright && playwright install msedge (or chromium; set browser.channel in the config).
"""
import os
import time


def _profile(cfg):
    p = os.path.expanduser((cfg.get('browser') or {}).get('profileDir') or '.pp-playbook/pw-profile')
    return p if os.path.isabs(p) else os.path.join(os.path.expanduser('~'), p)


def play_url(cfg, app_id, query=''):
    return '%s/play/e/%s/a/%s?hidenavbar=true%s' % (cfg.hosts['player'].rstrip('/'), cfg['environmentId'], app_id,
                                                    ('&' + query) if query else '')


class App(object):
    def __init__(self, ctx, url):
        self.page = ctx.new_page()
        self.page.goto(url, wait_until='domcontentloaded')
        self.frame = None

    def ready(self, first_control, timeout=150):
        t0 = time.time()
        while time.time() - t0 < timeout:
            consent = self.page.locator('iframe[name^="consentService-iFrame"]')
            if consent.count() > 0:
                try:
                    self.page.frame_locator('iframe[name^="consentService-iFrame"]').get_by_role('button', name='Allow', exact=True).click(timeout=3000)
                    time.sleep(3)
                    continue
                except Exception:
                    pass
            self.frame = self.page.frame(name='fullscreen-app-host')
            if self.frame is not None:
                try:
                    if self.c(first_control).is_visible(timeout=1000):
                        return self
                except Exception:
                    pass
            time.sleep(2)
        raise TimeoutError('%s never became visible' % first_control)

    def c(self, name, nth=0):
        loc = self.frame.locator('[data-control-name="%s"]' % name)
        return loc.nth(nth) if nth else loc.first

    def click(self, name, settle=1.5):
        self.c(name).click()
        time.sleep(settle)

    def fill(self, name, text, settle=0.5):
        box = self.c(name).locator('input, textarea').first
        box.click()
        box.fill(text)
        box.press('Tab')
        time.sleep(settle)

    def text(self, name):
        return (self.c(name).inner_text() or '').strip()

    def wait_text(self, name, predicate, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if predicate(self.text(name)):
                    return True
            except Exception:
                pass
            time.sleep(1)
        raise TimeoutError('%s: condition not met in %ds (last text %r)' % (name, timeout, self.text(name)))

    def shot(self, path, settle=1.5):
        vp = self.page.viewport_size or {'width': 1600, 'height': 900}
        self.page.mouse.move(vp['width'] - 2, vp['height'] - 2)
        time.sleep(settle)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.page.screenshot(path=path)
        return path


def browser(cfg):
    from playwright.sync_api import sync_playwright   # optional dependency
    b = cfg.get('browser') or {}
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(_profile(cfg), channel=b.get('channel') or None, headless=False,
                                               viewport=b.get('viewport') or {'width': 1600, 'height': 900},
                                               service_workers='block')
    return p, ctx


def capture_runtime(cfg, app_id, out_dir, wait_seconds=45):
    """Load the app and save every /appruntime/ response body (the compiled runtime JS) into out_dir."""
    p, ctx = browser(cfg)
    got = []
    try:
        page = ctx.new_page()
        resps = []
        page.on('response', resps.append)
        page.goto(play_url(cfg, app_id), wait_until='domcontentloaded')
        page.wait_for_timeout(wait_seconds * 1000)
        os.makedirs(out_dir, exist_ok=True)
        for r in resps:
            if '/appruntime/' in r.url and r.request.resource_type not in ('image', 'font', 'stylesheet'):
                try:
                    body = r.body()
                except Exception:
                    continue
                path = os.path.join(out_dir, 'runtime-%03d.js' % len(got))
                with open(path, 'wb') as fh:
                    fh.write(body)
                got.append(path)
    finally:
        ctx.close()
        p.stop()
    return got
