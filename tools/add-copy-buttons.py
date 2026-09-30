#!/usr/bin/env python3
"""Give every <pre> code block in an HTML doc a "Copy" button (people copy commands out of these pages).

Injects one marked <style>+<script> block before </body>. Idempotent: a file that already carries
the block has it replaced with the current version, so re-running after an edit is safe. The
script is plain inline JS (no external assets), styled from the page's own CSS variables when the
runbook theme defines them (--panel/--grid/--cyan/--muted), otherwise neutral dark colours.

Copying: navigator.clipboard where the page is a secure context (Edge treats file:// pages opened
opened locally as one), else a hidden-textarea execCommand('copy') fallback. The copied text is the
block's text WITHOUT the button label.

Usage:
  python tools/add-copy-buttons.py <file.html> [...]
  python tools/add-copy-buttons.py --all                # every tracked HTML with a <pre>
  python tools/add-copy-buttons.py --check <files...>   # exit 1 if any <pre> page lacks it
  python tools/add-copy-buttons.py --self-test
"""
import pathlib
import re
import subprocess
import sys

MARK_START = "<!-- copy-buttons:start (tools/add-copy-buttons.py) -->"
MARK_END = "<!-- copy-buttons:end -->"

BLOCK = MARK_START + r"""
<style>
  pre.has-copy { position: relative; padding-right: 4.8rem; }
  pre.has-copy > button.copy-btn {
    position: absolute; top: .4rem; right: .4rem; margin: 0;
    font: 600 .72rem/1 Consolas, ui-monospace, monospace; letter-spacing: .08em; text-transform: uppercase;
    color: var(--cyan, #7fb3e6); background: var(--bg, #0f1419); border: 1px solid var(--grid, #2c323b);
    border-radius: 3px; padding: .32rem .55rem; cursor: pointer; opacity: .8;
  }
  pre.has-copy > button.copy-btn:hover { opacity: 1; border-color: var(--cyan, #7fb3e6); }
  pre.has-copy > button.copy-btn.done { color: var(--olive, #9AA86A); border-color: var(--olive, #9AA86A); opacity: 1; }
  @media print { pre.has-copy > button.copy-btn { display: none; } }
</style>
<script>
(function () {
  function textOf(pre, btn) {
    var out = '';
    pre.childNodes.forEach(function (n) { if (n !== btn) { out += n.textContent; } });
    return out.replace(/\s+$/, '');
  }
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '-1000px';
    document.body.appendChild(ta); ta.select();
    var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  function flash(btn, label, cls) {
    btn.textContent = label; if (cls) { btn.classList.add(cls); }
    setTimeout(function () { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1600);
  }
  document.querySelectorAll('pre').forEach(function (pre) {
    if (pre.classList.contains('has-copy')) { return; }
    pre.classList.add('has-copy');
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'copy-btn'; btn.textContent = 'Copy';
    btn.title = 'Copy to clipboard';
    btn.addEventListener('click', function () {
      var text = textOf(pre, btn);
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { flash(btn, 'Copied', 'done'); },
          function () { flash(btn, fallbackCopy(text) ? 'Copied' : 'Select + Ctrl+C', fallbackCopy(text) ? 'done' : ''); });
      } else {
        var ok = fallbackCopy(text);
        flash(btn, ok ? 'Copied' : 'Select + Ctrl+C', ok ? 'done' : '');
      }
    });
    pre.appendChild(btn);
  });
})();
</script>
""" + MARK_END

EXCLUDE = re.compile(r"(^|/)(\.tmp|out|fixtures|mocks|debug|node_modules)/|golden|\.dc\.html$", re.I)


def inject(path):
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    if "<pre" not in text:
        return "skip (no <pre>)"
    if MARK_START in text:
        start = text.index(MARK_START)
        end = text.index(MARK_END, start) + len(MARK_END)
        new = text[:start] + BLOCK + text[end:]
        verb = "updated"
    else:
        idx = text.lower().rfind("</body>")
        new = (text[:idx] + BLOCK + "\n" + text[idx:]) if idx >= 0 else (text + "\n" + BLOCK + "\n")
        verb = "added"
    if new == text:
        return "unchanged"
    p.write_text(new, encoding="utf-8", newline="")
    return verb


def tracked_html():
    out = subprocess.run(["git", "ls-files", "*.html"], capture_output=True, text=True, check=True).stdout
    files = []
    for f in out.splitlines():
        f = f.strip()
        if not f or EXCLUDE.search(f):
            continue
        if "<pre" in pathlib.Path(f).read_text(encoding="utf-8", errors="replace"):
            files.append(f)
    return files


def self_test():
    import tempfile, os
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.html")
        pathlib.Path(p).write_text("<html><body><pre>echo hi</pre></body></html>", encoding="utf-8")
        first = inject(p)
        once = pathlib.Path(p).read_text(encoding="utf-8")
        second = inject(p)
        ok = first == "added" and second == "unchanged" and once.count(MARK_START) == 1 and once.index(MARK_START) < once.index("</body>")
        q = os.path.join(d, "b.html")
        pathlib.Path(q).write_text("<html><body><p>no code</p></body></html>", encoding="utf-8")
        ok = ok and inject(q).startswith("skip") and main(["--check", p]) == 0
    print(("  ok   " if ok else "  FAIL ") + "block injected once before </body>, idempotent, pages without <pre> skipped")
    print("self-test: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    if argv and argv[0] == "--self-test":
        return self_test()
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--check":
        missing = [f for f in argv[1:] if "<pre" in pathlib.Path(f).read_text(encoding="utf-8") and MARK_START not in pathlib.Path(f).read_text(encoding="utf-8")]
        for f in missing:
            print("MISSING copy buttons:", f)
        return 1 if missing else 0
    files = tracked_html() if argv[0] == "--all" else argv
    for f in files:
        print("%-9s %s" % (inject(f), f))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
