#!/usr/bin/env python3
"""
calibrate.py -- diff our renderer's output against a captured Studio DOM.

    python calibrate.py <reference.html> <screen.pa.yaml> [--sample-data samples.json]
                         [--catalog defaults-catalog.json] [--top N]

`reference.html` is a Studio-exported outerHTML dump of the live app (e.g. saved
from DevTools while previewing/running the app) -- NOT a screenshot. It's expected
to look like a DevTools outerHTML capture of a running screen (reference-canvas.html):
a tree of `<div data-control-name="X" style="position:absolute;top:...;left:...;
width:...;height:...">` nodes, nested the way Power Apps actually nests containers
(a child's top/left is relative to its own parent control, not the screen root).

WHAT THIS DOES
  1. Parses the reference DOM (ReferenceDOMParser below), accumulating each
     control's ABSOLUTE top/left by summing every positioned ancestor's own
     top/left from the screen root down to that control -- see the parser's
     docstring for exactly how, and for the scale()-transform handling.
  2. Renders the SAME screen with our own renderer, at the reference's own
     measured root size (not the authored size -- see "ROOT SIZE" below), and
     reads back the exact per-control geometry it resolved (Report.geometry --
     no HTML re-parsing needed on our side, since we control that format).
  3. Matches controls by name (the reference's data-control-name set is almost
     always a small subset of everything we render -- diffed honestly as such,
     never silently treated as 100% coverage).
  4. For each matched control: position/size deltas in px, a font-size delta (unit-
     aware: the reference mixes `Npt` on Labels with `Npx` on Fluent buttons/inputs;
     both are converted to px before comparing, using the same Size*4/3 rule our
     renderer itself uses), and an RGB color-mismatch flag+distance.
  5. Sorts by a combined severity score and prints the worst offenders.
  6. For any diffed property that OUR renderer resolved via the defaults-catalog
     ladder (not an explicit .pa.yaml value), checks whether the SAME delta shows
     up on other controls sharing that Control@Version -- if so, prints a
     ready-to-paste `DOCUMENTED` entry for build-catalog.py's corrections table.

ROOT SIZE: the reference capture measures 1698x798, not the app's authored
1366x768 (DocumentLayoutWidth/Height) -- see the calibration
section for the full reasoning, but in short: Power Apps resizes a screen's
Parent.Width/Height to whatever the live host viewport actually is (here, the
Studio preview pane on the user's 16" laptop), and since virtually every control
in this codebase positions itself relative to Parent.Width/Height, the screen
genuinely reflows to that size -- it is NOT a uniform zoom of the authored canvas
(1698/1366 != 798/768, so a single scale factor couldn't produce this even if one
were applied). Comparing meaningfully therefore means rendering OUR tool at the
reference's own measured 1698x798 (via --root-size), not at the authored size.
This tool does that automatically by reading the reference's own root landmark.

SCALE-TRANSFORM DEFENSE: independently of the above, this tool also detects any
CSS `transform: ... scale(N)` on an ancestor of the real control tree (a genuine
zoom, e.g. from a screenshot-based capture pipeline) and divides it back out of
every measurement before comparing. In the shipped reference-canvas.html, the only
scale() transform present (scale(1.11111), on a `canvasDecorationsContainer`) sits
inside a `display:none` editor-adorner branch that is NOT an ancestor of
`scrTaskWorkspace-container` at all -- so the detected ancestor_scale for that file
is 1.0 and nothing gets rescaled. This code path is exercised by the golden test
fixture (tests/fixtures/reference-scaled.html), which DOES wrap its real control
tree in a scale(1.25) ancestor.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capa_common import load_catalog  # noqa: E402

import importlib.util as _ilu  # noqa: E402
_rs_spec = _ilu.spec_from_file_location("_rs_for_calibrate", Path(__file__).resolve().parent / "render-screen.py")
_rs = _ilu.module_from_spec(_rs_spec)
_rs_spec.loader.exec_module(_rs)
render_screen = _rs.render_screen

VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"}

_STYLE_PROP_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+?)\s*(?:;|$)")
_PX_RE = re.compile(r"^(-?[\d.]+)px$")
_PT_RE = re.compile(r"^(-?[\d.]+)pt$")
_SCALE_RE = re.compile(r"scale\(\s*([\d.]+)\s*\)")
_RGB_RE = re.compile(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")

HARVEST_PROPS = ("font-size", "color", "background-color", "border-color", "font-weight")


def parse_style(style_str):
    return {m.group(1).strip().lower(): m.group(2).strip() for m in _STYLE_PROP_RE.finditer(style_str or "")}


def to_px(value):
    """'172px' -> 172.0 ; '15pt' -> 20.0 (pt*4/3, the same rule render-screen.py uses
    for its own Size property) ; anything else -> None."""
    if value is None:
        return None
    v = value.strip()
    m = _PX_RE.match(v)
    if m:
        return float(m.group(1))
    m = _PT_RE.match(v)
    if m:
        return float(m.group(1)) * 4 / 3
    return None


def parse_scale(transform_value):
    if not transform_value:
        return 1.0
    m = _SCALE_RE.search(transform_value)
    return float(m.group(1)) if m else 1.0


def parse_color(value):
    """'rgb(r,g,b)' / 'rgba(r,g,b,a)' / '#rrggbb' -> (r,g,b) int tuple, else None."""
    if not value:
        return None
    v = value.strip()
    m = _RGB_RE.match(v)
    if m:
        return tuple(int(round(float(x))) for x in m.groups())
    m = _HEX_RE.match(v)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return None


class ReferenceDOMParser(HTMLParser):
    """See module docstring. Produces:
        .controls[name] = {name, abs_top, abs_left, width, height, z_index,
                            display, style: {font-size,color,background-color,
                                              border-color,font-weight}}
        .order            -- names in first-seen (document) order
        .root_w/.root_h   -- the reference's own measured screen size (from the
                              'animated-canvas-dummy-sizer' landmark, read BEFORE
                              any control so it's unaffected by anything below)
        .ancestor_scale   -- cumulative product of transform:scale(N) found on any
                              ancestor of the FIRST data-control-name node reached
                              (1.0 if none -- the normal case for a live DOM dump)
    """

    def __init__(self):
        super().__init__()
        self.frames = []  # {'offset': (top,left), 'is_control': bool, 'name': str|None}
        self.cur_offset = (0.0, 0.0)
        self.controls = {}
        self.order = []
        self.ancestor_scale = 1.0
        self._found_first_control = False
        self._pending_scale_product = 1.0
        self.root_w = None
        self.root_h = None

    def _innermost_control(self):
        for f in reversed(self.frames):
            if f["is_control"]:
                return f["name"]
        return None

    def _open(self, tag, attrs, void):
        attrs = dict(attrs)
        style = parse_style(attrs.get("style", ""))

        if not self._found_first_control:
            sc = parse_scale(style.get("transform"))
            if sc != 1.0:
                self._pending_scale_product *= sc
            if self.root_w is None and "animated-canvas-dummy-sizer" in attrs.get("class", ""):
                self.root_w = to_px(style.get("width"))
                self.root_h = to_px(style.get("height"))

        top, left = to_px(style.get("top")), to_px(style.get("left"))
        cur_top, cur_left = self.cur_offset
        new_offset = (cur_top + top, cur_left + left) if (top is not None and left is not None) else self.cur_offset

        name = attrs.get("data-control-name") if tag == "div" else None
        is_control = bool(name)
        if is_control:
            if not self._found_first_control:
                self.ancestor_scale = self._pending_scale_product
                self._found_first_control = True
            self.controls[name] = {
                "name": name, "abs_top": new_offset[0], "abs_left": new_offset[1],
                "width": to_px(style.get("width")), "height": to_px(style.get("height")),
                "z_index": style.get("z-index"), "display": style.get("display"),
                "style": {},
            }
            self.order.append(name)
        else:
            inner = self._innermost_control()
            if inner is not None:
                entry = self.controls[inner]["style"]
                for prop in HARVEST_PROPS:
                    if prop in style and prop not in entry:
                        entry[prop] = style[prop]

        if not void:
            self.cur_offset = new_offset
            self.frames.append({"offset": new_offset, "is_control": is_control, "name": name})

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, void=(tag in VOID_ELEMENTS))

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, void=True)

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        if self.frames:
            self.frames.pop()
            self.cur_offset = self.frames[-1]["offset"] if self.frames else (0.0, 0.0)


def parse_reference(path):
    parser = ReferenceDOMParser()
    parser.feed(Path(path).read_text(encoding="utf-8"))
    scale = parser.ancestor_scale
    if scale != 1.0:
        # Defensive normalization for a genuine ancestor zoom (not needed for the
        # shipped reference-canvas.html -- see module docstring -- but exercised by
        # tests/fixtures/reference-scaled.html).
        for c in parser.controls.values():
            for k in ("abs_top", "abs_left", "width", "height"):
                if c[k] is not None:
                    c[k] = c[k] / scale
            fs = c["style"].get("font-size")
            px_val = to_px(fs)
            if px_val is not None:
                c["style"]["font-size"] = f"{px_val / scale}px"
        if parser.root_w:
            parser.root_w /= scale
        if parser.root_h:
            parser.root_h /= scale
    return parser


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

class Diff:
    __slots__ = ("name", "key", "dx", "dy", "dw", "dh", "d_font", "ref_color", "our_color",
                 "color_mismatch", "ref_display", "our_display_hidden", "severity", "notes")

    def to_row(self):
        parts = []
        if self.dx is not None and abs(self.dx) > 0.5:
            parts.append(f"x {self.dx:+.1f}px")
        if self.dy is not None and abs(self.dy) > 0.5:
            parts.append(f"y {self.dy:+.1f}px")
        if self.dw is not None and abs(self.dw) > 0.5:
            parts.append(f"w {self.dw:+.1f}px")
        if self.dh is not None and abs(self.dh) > 0.5:
            parts.append(f"h {self.dh:+.1f}px")
        if self.d_font is not None and abs(self.d_font) > 0.3:
            parts.append(f"font {self.d_font:+.1f}px")
        if self.color_mismatch:
            parts.append(f"color ref={self.ref_color} ours={self.our_color}")
        if self.notes:
            parts.append(self.notes)
        return f"{self.name:<22} ({self.key:<22}) sev={self.severity:6.1f}  " + ", ".join(parts)


def diff_control(name, ref, geo):
    d = Diff()
    d.name = name
    d.key = geo.get("key", "?")
    # Compare ABSOLUTE positions on both sides: the reference parser accumulates
    # top/left through every nested ancestor (see ReferenceDOMParser), and our own
    # renderer's Report.geometry stores the matching 'abs_x'/'abs_y' it computed
    # while walking the same nesting (its 'x'/'y' are parent-LOCAL, used for CSS --
    # comparing those directly against the reference's absolute values would look
    # like a huge, spurious diff on every nested control).
    d.dx = (geo["abs_x"] - ref["abs_left"]) if geo.get("abs_x") is not None and ref.get("abs_left") is not None else None
    d.dy = (geo["abs_y"] - ref["abs_top"]) if geo.get("abs_y") is not None and ref.get("abs_top") is not None else None
    d.dw = (geo["w"] - ref["width"]) if geo.get("w") is not None and ref.get("width") is not None else None
    d.dh = (geo["h"] - ref["height"]) if geo.get("h") is not None and ref.get("height") is not None else None

    ref_font_px = to_px(ref["style"].get("font-size"))
    our_font_px = geo.get("size_px")
    d.d_font = (our_font_px - ref_font_px) if (ref_font_px is not None and our_font_px is not None) else None

    ref_color = parse_color(ref["style"].get("color"))
    our_color = parse_color(geo.get("color")) if geo.get("color") else None
    d.ref_color, d.our_color = ref_color, our_color
    d.color_mismatch = bool(ref_color and our_color and
                             sum(abs(a - b) for a, b in zip(ref_color, our_color)) > 12)

    d.ref_display = ref.get("display")
    d.our_display_hidden = None  # populated by caller if it knows (layer panel state)
    d.notes = ""

    color_penalty = 15 if d.color_mismatch else 0
    d.severity = (abs(d.dx or 0) + abs(d.dy or 0) + abs(d.dw or 0) + abs(d.dh or 0) +
                  2 * abs(d.d_font or 0) + color_penalty)
    return d


def run_calibration(reference_path, screen_path, sample_data_path=None, catalog_path=None, var_overrides=None):
    ref = parse_reference(reference_path)
    root_size = None
    if ref.root_w and ref.root_h:
        root_size = (ref.root_w, ref.root_h)

    _doc_html, report = render_screen(screen_path, sample_data_path=sample_data_path, catalog_path=catalog_path,
                                       var_overrides=var_overrides, root_size_override=root_size)
    catalog = load_catalog(catalog_path)

    diffs = []
    missing_in_ours = []
    screen_name, _ = _rs.get_screen(_rs.load_pa_yaml(screen_path))
    for name, refc in ref.controls.items():
        if name == screen_name:
            continue  # the screen root itself isn't a trackable "control" on our side
        geo = report.geometry.get(name)
        if geo is None:
            missing_in_ours.append(name)
            continue
        diffs.append(diff_control(name, refc, geo))
    diffs.sort(key=lambda d: -d.severity)

    return {
        "ref": ref, "report": report, "catalog": catalog, "diffs": diffs,
        "missing_in_ours": missing_in_ours, "root_size": root_size,
    }


# ---------------------------------------------------------------------------
# Systematic-miss tracing -> ready-to-paste DOCUMENTED proposals
# ---------------------------------------------------------------------------

def _catalog_source_for(report, name, prop):
    for (n, k, p, v, src, reason, matched_key) in report.defaulted:
        if n == name and p == prop:
            return src, k
    for (n, k, p, v, reason) in report.unresolved:
        if n == name and p == prop:
            return "fallback", k
    return None, None


def propose_corrections(result, min_group=2):
    """For each (Control@Version, property) where >=min_group diffed controls share
    the SAME catalog-sourced property and a consistent (within 1px/1pt) delta,
    propose a corrected catalog value: the value that would have made OUR resolved
    value match the reference (i.e. our_catalog_value - delta)."""
    report = result["report"]
    diffs_by_name = {d.name: d for d in result["diffs"]}

    # property -> (dx-like delta accessor)
    prop_delta = {
        "Width": lambda d: d.dw, "Height": lambda d: d.dh,
        "X": lambda d: d.dx, "Y": lambda d: d.dy,
    }

    groups = defaultdict(list)  # (control_key, prop) -> [(name, delta)]
    for name, d in diffs_by_name.items():
        for prop, getter in prop_delta.items():
            delta = getter(d)
            if delta is None or abs(delta) < 1.0:
                continue
            src, matched_key = _catalog_source_for(report, name, prop)
            if src is None:
                continue  # this property was explicit in the .pa.yaml -- not a catalog issue
            groups[(matched_key or d.key, prop, src)].append((name, delta))

    proposals = []
    for (ckey, prop, src), entries in groups.items():
        if len(entries) < min_group:
            continue
        deltas = [e[1] for e in entries]
        spread = max(deltas) - min(deltas)
        if spread > 1.5:
            continue  # not consistent enough to call it one systematic catalog error
        avg_delta = sum(deltas) / len(deltas)
        # our resolved (catalog) value minus the delta = what the catalog SHOULD say
        catalog = result["catalog"]
        cur = catalog.get(ckey, {}).get(prop, {}).get("value")
        if cur is None:
            continue
        corrected = round(cur - avg_delta, 1)
        if float(corrected).is_integer():
            corrected = int(corrected)
        names = ", ".join(n for n, _ in entries)
        proposals.append(
            f'  # {len(entries)} controls ({names}) all show {prop} off by {avg_delta:+.1f}px '
            f'(current catalog source: {src})\n'
            f'  "{ckey}": {{"{prop}": {corrected}}},  # was {cur}'
        )
    return proposals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reference", help="Studio-exported outerHTML dump (e.g. reference-canvas.html)")
    ap.add_argument("screen", help="the matching screen .pa.yaml source")
    ap.add_argument("--sample-data")
    ap.add_argument("--catalog")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    result = run_calibration(args.reference, args.screen, args.sample_data, args.catalog)
    ref, report, diffs = result["ref"], result["report"], result["diffs"]

    print(f"Reference: {args.reference}")
    print(f"  {len(ref.controls)} data-control-name nodes captured; root size "
          f"{ref.root_w:.0f}x{ref.root_h:.0f}; ancestor scale factor detected: {ref.ancestor_scale:.5f}"
          + ("" if ref.ancestor_scale == 1.0 else " (normalized out before comparing)"))
    print(f"Rendered:  {args.screen}")
    print(f"  {report.counts()['controls']} controls rendered total "
          f"(coverage: {len(diffs)}/{len(ref.controls)} reference controls matched by name; "
          f"diffing only what the reference captured)")
    if result["missing_in_ours"]:
        print(f"  NOT found in our render (name mismatch?): {result['missing_in_ours']}")

    print(f"\nTop {min(args.top, len(diffs))} diffs by severity:")
    for d in diffs[:args.top]:
        print("  " + d.to_row())

    zero = [d for d in diffs if d.severity < 1.0]
    print(f"\n{len(zero)}/{len(diffs)} matched controls are pixel-exact (severity < 1.0).")

    proposals = propose_corrections(result)
    if proposals:
        print(f"\n{len(proposals)} unambiguous systematic-miss correction(s) "
              f"(paste into build-catalog.py's DOCUMENTED dict for the relevant key):")
        for p in proposals:
            print(p)
    else:
        print("\nNo unambiguous systematic corrections found (no property+delta pair recurred "
              "consistently across >=2 controls of the same catalog-sourced template).")


if __name__ == "__main__":
    main()
