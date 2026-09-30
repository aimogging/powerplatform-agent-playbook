#!/usr/bin/env python3
"""
Golden tests for the canvas layout renderer (render-screen.py, capa_common.py, calibrate.py).

Run directly:  python tests/test_canvas_design.py
Or:            python -m unittest discover -s tests -p "test_*.py" -v

No external test framework required (stdlib unittest; PyYAML is needed by capa_common).
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

TOOLKIT_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(TOOLKIT_DIR))


def _load(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, TOOLKIT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load("rs_under_test", "render-screen.py")
import calibrate  # noqa: E402 -- no hyphen in the filename, importable directly
import capa_common as cc  # noqa: E402
from capa_common import find_authored_size  # noqa: E402


def get_div(doc_html, name):
    """Return the opening <div ...> tag for the control with this data-name, or None."""
    m = re.search(r'<div class="([^"]*)" style="([^"]*)"[^>]*data-name="' + re.escape(name) + r'"[^>]*>', doc_html)
    if not m:
        return None
    return {"classes": m.group(1).split(), "style": m.group(2)}


def style_px(style, prop):
    m = re.search(re.escape(prop) + r':([\-0-9.]+)px', style)
    return float(m.group(1)) if m else None


class RenderBasicTest(unittest.TestCase):
    def setUp(self):
        self.html, self.report = rs.render_screen(FIXTURES / "basic.pa.yaml")

    def test_group_container_box(self):
        d = get_div(self.html, "conBox")
        self.assertIsNotNone(d, "conBox div not found")
        self.assertEqual(style_px(d["style"], "left"), 50)
        self.assertEqual(style_px(d["style"], "top"), 60)
        self.assertEqual(style_px(d["style"], "width"), 300)
        self.assertEqual(style_px(d["style"], "height"), 200)
        self.assertIn("border:2.0px solid #333333", d["style"])
        self.assertNotIn("flagged", d["classes"])

    def test_theme_color_resolved(self):
        d = get_div(self.html, "conBox")
        # Fill: =varTheme.Bg -> "#ffffff" from tests/fixtures/App.pa.yaml
        self.assertIn("background:#ffffff", d["style"])

    def test_label_parent_relative_width(self):
        d = get_div(self.html, "lblHello")
        self.assertIsNotNone(d)
        # Width: =Parent.Width-20, Parent (conBox) resolved width = 300 -> 280
        self.assertEqual(style_px(d["style"], "width"), 280)
        self.assertEqual(style_px(d["style"], "left"), 10)
        self.assertEqual(style_px(d["style"], "top"), 10)
        self.assertNotIn("flagged", d["classes"])

    def test_label_text_and_color(self):
        self.assertIn("Hello World", self.html)
        d = get_div(self.html, "lblHello")
        self.assertNotIn("varTheme", d["style"])  # sanity: no raw formula text leaked into style
        self.assertIn("#4750e0", self.html)  # varTheme.Accent resolved

    def test_rectangle_rgba_fill(self):
        d = get_div(self.html, "recTick")
        self.assertIsNotNone(d)
        self.assertIn("background:rgba(255,0,0,1)", d["style"])
        self.assertEqual(style_px(d["style"], "width"), 100)
        self.assertEqual(style_px(d["style"], "height"), 4)

    def test_no_unknown_controls(self):
        self.assertEqual(self.report.counts()["unknown_controls"], 0)


class MultilineTextInputTest(unittest.TestCase):
    def test_classic_multiline_wraps_and_uses_padding(self):
        html, _ = rs.render_screen(FIXTURES / 'multiline-input.pa.yaml', var_overrides={'varMultilineFixture': 'First requirement.\nSecond requirement with enough words to wrap inside the field.'})
        multiline = html.split('data-name="txtMultiline"', 1)[1].split('data-name="txtSingleLine"', 1)[0]
        self.assertIn('white-space:pre-wrap', multiline)
        self.assertIn('overflow-wrap:anywhere', multiline)
        self.assertIn('line-height:1.35', multiline)
        self.assertRegex(multiline, r'padding:8(?:\.0)?px 12(?:\.0)?px 8(?:\.0)?px 12(?:\.0)?px')
        self.assertIn('First requirement.\nSecond requirement', multiline)
        single = html.split('data-name="txtSingleLine"', 1)[1].split('</div>', 1)[0]
        self.assertIn('white-space:nowrap', single)
        self.assertNotIn('white-space:pre-wrap', single)


class ButtonAppearanceTest(unittest.TestCase):
    """ModernButton.Appearance drives the chrome the catalog would otherwise default.

    The catalog's ModernButton entry is the PRIMARY look (#242424 fill). Without
    the Appearance rule, `Appearance: =ButtonAppearance.Transparent` overlay
    buttons -- the technique this corpus uses to make gallery rows and tiles
    tappable -- paint as opaque dark rectangles over the design underneath.
    """

    def setUp(self):
        self.html, self.report = rs.render_screen(FIXTURES / "basic.pa.yaml")

    def test_primary_button_keeps_catalog_fill(self):
        d = get_div(self.html, "btnPrimary")
        self.assertIsNotNone(d)
        self.assertIn("background:#242424", d["style"])

    def test_transparent_overlay_button_has_no_fill_and_no_border(self):
        d = get_div(self.html, "btnOverlay")
        self.assertIsNotNone(d)
        self.assertIn("background:transparent", d["style"])
        self.assertIn("border:none", d["style"])

    def test_outline_button_is_transparent_with_a_palette_border(self):
        d = get_div(self.html, "btnOutline")
        self.assertIsNotNone(d)
        self.assertIn("background:transparent", d["style"])
        self.assertIn("border:1.0px solid #4750e0", d["style"])

    def test_primary_button_takes_its_fill_from_base_palette_color(self):
        d = get_div(self.html, "btnPaletteFilled")
        self.assertIsNotNone(d)
        self.assertIn("background:#1f7a43", d["style"])

    def test_explicit_fill_beats_the_appearance_rule(self):
        d = get_div(self.html, "btnOutlineExplicitFill")
        self.assertIsNotNone(d)
        self.assertIn("background:#eceefc", d["style"])


class SeededInputValueTest(unittest.TestCase):
    """A Modern text input / dropdown authored with `Default` renders that value.

    `Default` is the seed property both Modern controls use in this corpus
    (srcBuilder / scrTaskWorkspace, live-import proven). The dropdown branch used
    to hardcode the "Select an item" empty state, so a filter builder mock whose
    rules were fully seeded still reviewed as a screen full of empty pickers.
    Placeholders must survive for genuinely unseeded controls.
    """

    def setUp(self):
        self.html, self.report = rs.render_screen(FIXTURES / "seeded-inputs.pa.yaml")

    def inner(self, name):
        m = re.search(r'<div class="[^"]*" style="[^"]*"[^>]*data-name="' + re.escape(name) + r'"[^>]*>',
                      self.html)
        self.assertIsNotNone(m, f"{name} div not found")
        return self.html[m.end():m.end() + 500]

    def test_seeded_text_input_shows_its_default(self):
        body = self.inner("txtSeeded")
        self.assertIn("Publication emails", body)

    def test_unseeded_text_input_still_shows_its_placeholder(self):
        body = self.inner("txtUnseeded")
        self.assertIn("Filter name", body)

    def test_seeded_dropdown_shows_its_default_not_the_empty_state(self):
        body = self.inner("ddSeeded")
        self.assertIn("anyOf", body)
        self.assertNotIn("Select an item", body)

    def test_unseeded_dropdown_still_shows_the_empty_state(self):
        body = self.inner("ddUnseeded")
        self.assertIn("Select an item", body)


class TemplateSizeEvalTest(unittest.TestCase):
    """Inside a gallery row the renderer passes the row box as the parent, so
    Parent.TemplateWidth/TemplateHeight are the row's own dimensions (2026-09-28:
    every row control sized by Parent.TemplateWidth used to fall back to 100px)."""

    def test_template_width_and_height_are_the_row_box(self):
        ctx = cc.EvalContext(parent_w=392, parent_h=64)
        self.assertEqual(cc.eval_formula("=Parent.TemplateWidth - 20", ctx)[0], 372)
        self.assertEqual(cc.eval_formula("=Parent.TemplateHeight", ctx)[0], 64)


class StringFunctionEvalTest(unittest.TestCase):
    """Left/Right/Mid/Upper/Lower/Concatenate/Text(1-arg) over already-resolved values.

    Row text in this corpus is almost always Left(ThisItem.x, n) or Upper(...);
    without these the renderer shows "(dynamic text)" for every list row, which is
    exactly the truncation/typography the render is supposed to let you review.
    A formatted Text(value, "dd mmm yyyy") stays UNRESOLVED on purpose -- the
    evaluator does not model format strings and must not invent a date.
    """

    def ev(self, formula, this_item=None):
        ctx = cc.EvalContext(this_item=this_item or {})
        return cc.eval_formula(formula, ctx)[0]

    def test_left_and_right(self):
        self.assertEqual(self.ev('=Left("abcdef",3)'), "abc")
        self.assertEqual(self.ev('=Right("abcdef",2)'), "ef")

    def test_mid(self):
        self.assertEqual(self.ev('=Mid("abcdef",2,3)'), "bcd")
        self.assertEqual(self.ev('=Mid("abcdef",4)'), "def")

    def test_upper_lower_and_concatenate(self):
        self.assertEqual(self.ev('=Upper("ab")'), "AB")
        self.assertEqual(self.ev('=Lower("AB")'), "ab")
        self.assertEqual(self.ev('=Concatenate("a","b","c")'), "abc")

    def test_text_single_arg_stringifies(self):
        self.assertEqual(self.ev("=Text(4)"), "4")

    def test_formatted_text_stays_unresolved(self):
        self.assertIs(self.ev('=Text(4,"dd mmm yyyy")'), cc.UNRESOLVED)

    def test_unresolved_input_stays_unresolved(self):
        self.assertIs(self.ev("=Left(CountRows(colFoo),3)"), cc.UNRESOLVED)

    def test_left_over_this_item_field(self):
        self.assertEqual(self.ev("=Left(ThisItem.req,4)", {"req": "abcdefgh"}), "abcd")


class RenderDefaultsLadderTest(unittest.TestCase):
    def setUp(self):
        self.html, self.report = rs.render_screen(
            FIXTURES / "defaults.pa.yaml", catalog_path=FIXTURES / "mini-catalog.json")

    def test_omitted_height_resolves_from_catalog_and_is_not_flagged(self):
        d = get_div(self.html, "lblA")
        self.assertIsNotNone(d)
        self.assertEqual(style_px(d["style"], "height"), 40, "should take the mini-catalog Height=40")
        self.assertNotIn("flagged", d["classes"], "a catalog hit must NOT be visually flagged")
        # and it must show up in the silent 'defaulted' bucket, not 'unresolved'
        self.assertTrue(any(n == "lblA" and p == "Height" for (n, k, p, v, src, reason, mk) in self.report.defaulted))
        self.assertFalse(any(n == "lblA" and p == "Height" for (n, k, p, v, reason) in self.report.unresolved))

    def test_property_missing_from_catalog_is_flagged(self):
        d = get_div(self.html, "lblB")
        self.assertIsNotNone(d)
        # Width has no entry anywhere in mini-catalog.json -> hardcoded fallback (150) + flagged
        self.assertEqual(style_px(d["style"], "width"), 150)
        self.assertIn("flagged", d["classes"])
        self.assertTrue(any(n == "lblB" and p == "Width" for (n, k, p, v, reason) in self.report.unresolved))
        # its Height DOES resolve from the same catalog entry as lblA -- confirms the ladder
        # is evaluated per-property, not "once anything is missing, everything is fallback"
        self.assertEqual(style_px(d["style"], "height"), 40)


class RenderGalleryTest(unittest.TestCase):
    def setUp(self):
        self.html, self.report = rs.render_screen(
            FIXTURES / "gallery.pa.yaml", sample_data_path=FIXTURES / "gallery-sample.json")

    def test_three_rows_rendered(self):
        for i in range(3):
            self.assertIn(f'data-name="galRows-row{i}"', self.html)

    def test_row_vertical_offsets(self):
        # TemplateSize=50, TemplatePadding=10 -> step 60px
        for i, expected_top in enumerate((0, 60, 120)):
            m = re.search(r'<div class="ctrl" style="left:([\-0-9.]+)px;top:([\-0-9.]+)px;[^"]*"[^>]*data-name="galRows-row' + str(i) + '"', self.html)
            self.assertIsNotNone(m, f"row {i} wrapper not found")
            self.assertEqual(float(m.group(2)), float(expected_top))

    def test_row_content_from_sample_data(self):
        self.assertIn("Row A", self.html)
        self.assertIn("Row B", self.html)
        self.assertIn("Row C", self.html)

    def test_no_gallery_fallback_note(self):
        self.assertEqual(self.report.gallery_notes, [])


def layer_wrapper_style(doc_html, name):
    m = re.search(r'<div data-layer="' + re.escape(name) + r'" style="([^"]*)">', doc_html)
    return m.group(1) if m else None


def all_layer_wrapper_names(doc_html):
    return re.findall(r'<div data-layer="([^"]*)" style="[^"]*">', doc_html)


def all_group_checkboxes(doc_html):
    """Every grouped checkbox in the layer panel: [(checked: bool, [member names]), ...],
    in document order. A checkbox now drives a GROUP of one or more data-layer
    wrappers via its data-layer-group attribute (a comma-joined list of names), not
    a single name as an inline onclick argument."""
    out = []
    for m in re.finditer(
            r'<input type="checkbox"\s*(checked)?\s*data-layer-group="([^"]*)"\s*onchange="toggleLayerGroup\(this\)">',
            doc_html):
        members = m.group(2).split(",") if m.group(2) else []
        out.append((m.group(1) is not None, members))
    return out


def layer_group_for(doc_html, member_name):
    """The (checked, members) tuple for whichever group checkbox owns this control
    name, or None if no group claims it."""
    for checked, members in all_group_checkboxes(doc_html):
        if member_name in members:
            return checked, members
    return None


class BandedGalleryRowVisibleTest(unittest.TestCase):
    """A flattened two-template gallery must paint ONE template per row.

    Top-level controls paint regardless of Visible (the layer panel owns those),
    but inside a gallery row template a Visible that resolves to a concrete False
    against this row's ThisItem hides the control -- otherwise the band-header and
    task-row templates both paint on every row and the render is unreadable.
    """

    def setUp(self):
        self.html, self.report = rs.render_screen(
            FIXTURES / "gallery.pa.yaml", sample_data_path=FIXTURES / "gallery-sample.json")
        self.rows = re.findall(r'data-name="galBanded-row(\d+)"(.*?)(?=data-name="galBanded-row|\Z)',
                               self.html, re.S)

    def _row(self, i):
        return next(body for idx, body in self.rows if idx == str(i))

    def test_band_row_shows_only_the_header_template(self):
        body = self._row(0)
        self.assertIn("OVERDUE", body)
        self.assertNotIn('data-name="lblTaskRow"', body)

    def test_task_row_shows_only_the_task_template(self):
        body = self._row(1)
        self.assertIn("abcdef", body)          # Left(requirement,6)
        self.assertNotIn('data-name="lblBandHeader"', body)

    def test_control_with_no_visible_formula_paints_on_every_row(self):
        for i in (0, 1):
            self.assertIn('data-name="lblAlways"', self._row(i))

    def test_container_child_hidden_by_a_seeded_var_is_dropped(self):
        html, _ = rs.render_screen(FIXTURES / "layers.pa.yaml")
        # conVisibleSwap's two children are mutually exclusive on an OnStart-seeded
        # var: only the resolved-true one may paint. (Top-level layers still paint --
        # LayerPanelTest covers that contract.)
        self.assertIn('data-name="lblSwapOn"', html)     # Visible: =!varShowX  -> true
        self.assertNotIn('data-name="lblSwapOff"', html)  # Visible: =varShowX   -> false


class LayerPanelTest(unittest.TestCase):
    """Covers the OnStart-var-seeding fix + the GROUPED layer panel.

    Scrims/modals whose Visible formula depends on an OnStart-seeded var (e.g.
    Visible: =varShowX with Set(varShowX, false) in App.pa.yaml, or
    Visible: =Len(varWsAction)>0 with Set(varWsAction, "")) start hidden (unchecked,
    display:none) instead of painting on top of everything by default; every control
    still renders (nothing is suppressed from the DOM); --set overrides flip the
    initial state; an unresolvable Visible (unknown var) keeps the old
    default-to-shown behavior.

    Since the grouping fix, related top-level controls (layers.pa.yaml's
    scrimWsAlert/conWsAlert pair) share ONE checkbox via a data-layer-group
    attribute, and every control whose DEFAULT (pre-`--set`) state is visible
    (conMatrix, conUnknownVis, conVisibleSwap) collapses into a single always-on
    "Base screen" entry -- scrimHidden/conConfirmScrim/scrimWsAlert/conWsAlert
    default to hidden, so they stay out of it and keep their own group(s)."""

    def test_onstart_seeded_false_layer_starts_hidden(self):
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        style = layer_wrapper_style(html_doc, "scrimHidden")
        self.assertIsNotNone(style)
        self.assertIn("display:none", style)
        group = layer_group_for(html_doc, "scrimHidden")
        self.assertIsNotNone(group, "scrimHidden has no owning group checkbox")
        self.assertEqual(group[0], False)
        # it must still be painted (present in the DOM), just hidden via the wrapper
        self.assertIn('data-name="scrimHidden"', html_doc)

    def test_onstart_seeded_comparison_layer_starts_hidden(self):
        # Visible: =Len(varWsAction)>0, with Set(varWsAction, "") in OnStart -> False
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        style = layer_wrapper_style(html_doc, "conConfirmScrim")
        self.assertIn("display:none", style)
        self.assertEqual(layer_group_for(html_doc, "conConfirmScrim")[0], False)

    def test_no_visible_property_layer_starts_shown(self):
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        style = layer_wrapper_style(html_doc, "conMatrix")
        self.assertNotIn("display:none", style)
        self.assertEqual(layer_group_for(html_doc, "conMatrix")[0], True)

    def test_unknown_var_keeps_default_shown_behavior(self):
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        style = layer_wrapper_style(html_doc, "conUnknownVis")
        self.assertNotIn("display:none", style)
        self.assertEqual(layer_group_for(html_doc, "conUnknownVis")[0], True)

    def test_set_override_flips_initial_state(self):
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml", var_overrides={"varShowX": True})
        style = layer_wrapper_style(html_doc, "scrimHidden")
        self.assertNotIn("display:none", style)
        self.assertEqual(layer_group_for(html_doc, "scrimHidden")[0], True)

    def test_set_override_does_not_reshuffle_grouping(self):
        # Rendering with --set varShowX=true (previewing the "on" state) must not
        # itself pull scrimHidden/scrimWsAlert into the Base-screen collapse -- that
        # decision is made against the OnStart DEFAULT, not the current --set state,
        # so the panel's own structure stays stable across preview renders.
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml", var_overrides={"varShowX": True})
        base = next(g for g in all_group_checkboxes(html_doc) if "conMatrix" in g[1])
        self.assertNotIn("scrimHidden", base[1])
        self.assertNotIn("scrimWsAlert", base[1])

    def test_grouping_clusters_a_scrim_panel_pair_into_one_entry(self):
        # scrimWsAlert + conWsAlert share the cluster key ("Ws","Alert") (same
        # remaining suffix after stripping their scrim/con type prefix) -- they must
        # resolve to the SAME group checkbox, not two separate ones.
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        scrim_group = layer_group_for(html_doc, "scrimWsAlert")
        con_group = layer_group_for(html_doc, "conWsAlert")
        self.assertIsNotNone(scrim_group)
        self.assertIsNotNone(con_group)
        self.assertEqual(scrim_group[1], con_group[1])
        self.assertIn("scrimWsAlert", scrim_group[1])
        self.assertIn("conWsAlert", scrim_group[1])
        self.assertEqual(len(scrim_group[1]), 2, "the Alert cluster should have exactly 2 members")

    def test_base_screen_collapse(self):
        # conMatrix, conUnknownVis, conVisibleSwap are all visible in the DEFAULT
        # (OnStart) state and share no cluster key with one another -- they must
        # still collapse into ONE "Base screen" entry rather than three separate
        # single-member groups.
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        base = layer_group_for(html_doc, "conMatrix")
        self.assertIsNotNone(base)
        self.assertTrue(base[0], "Base screen should be checked by default")
        for name in ("conMatrix", "conUnknownVis", "conVisibleSwap"):
            self.assertIn(name, base[1])
        # default-hidden controls must NOT be swept into Base screen
        for name in ("scrimHidden", "conConfirmScrim", "scrimWsAlert", "conWsAlert"):
            self.assertNotIn(name, base[1])
        self.assertIn('<span class="layer-name">Base screen</span>', html_doc)

    def test_checkbox_wiring_matches_emitted_wrappers(self):
        # Pins the actual regression that shipped: a checkbox's data-layer-group
        # names must be exactly the set of data-layer wrapper divs actually emitted
        # for the DOM -- every group member resolves to a real wrapper (no dangling
        # checkbox target), and every wrapper is claimed by exactly one checkbox (no
        # orphaned layer the panel can't reach).
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        wrapper_names = set(all_layer_wrapper_names(html_doc))
        self.assertTrue(wrapper_names, "no data-layer wrappers were emitted at all")
        claimed = []
        for checked, members in all_group_checkboxes(html_doc):
            for name in members:
                self.assertIn(name, wrapper_names,
                              f"checkbox references '{name}' but no matching <div data-layer=\"{name}\"> wrapper "
                              f"was emitted -- the onclick/id wiring is broken")
                claimed.append(name)
        self.assertEqual(sorted(claimed), sorted(wrapper_names),
                          "every top-level data-layer wrapper must be owned by exactly one checkbox")
        self.assertEqual(len(claimed), len(set(claimed)), "a data-layer name is claimed by more than one checkbox")

    def test_layer_panel_lists_all_top_level_children(self):
        html_doc, _report = rs.render_screen(FIXTURES / "layers.pa.yaml")
        for name in ("conMatrix", "scrimHidden", "conConfirmScrim", "conUnknownVis", "scrimWsAlert", "conWsAlert"):
            self.assertIsNotNone(layer_group_for(html_doc, name), f"{name} is not reachable from any group checkbox")


class CalibrationParserTest(unittest.TestCase):
    """Golden test for calibrate.py's ReferenceDOMParser: a small handcrafted
    reference-DOM fixture with known expected boxes, exercising (a) absolute-position
    accumulation through nested containers and (b) scale-transform normalization.
    reference-basic.html and reference-scaled.html describe the SAME layout -- the
    scaled one wraps everything in a transform:scale(1.25) ancestor and has every
    pixel value pre-multiplied by 1.25 -- so both must parse to identical results."""

    EXPECTED = {
        "conBox": {"abs_top": 10.0, "abs_left": 20.0, "width": 200.0, "height": 150.0},
        "lblInside": {"abs_top": 15.0, "abs_left": 28.0, "width": 100.0, "height": 20.0,
                      "font_px": 16.0, "color": (10, 20, 30)},
        "lblOutside": {"abs_top": 200.0, "abs_left": 40.0, "width": 90.0, "height": 22.0,
                       "font_px": 11.0, "color": (1, 2, 3)},
    }

    def _check(self, path, expect_scale):
        ref = calibrate.parse_reference(path)
        self.assertAlmostEqual(ref.root_w, 400.0, places=3)
        self.assertAlmostEqual(ref.root_h, 300.0, places=3)
        self.assertAlmostEqual(ref.ancestor_scale, expect_scale, places=3)
        for name, exp in self.EXPECTED.items():
            c = ref.controls[name]
            self.assertAlmostEqual(c["abs_top"], exp["abs_top"], places=2, msg=name)
            self.assertAlmostEqual(c["abs_left"], exp["abs_left"], places=2, msg=name)
            self.assertAlmostEqual(c["width"], exp["width"], places=2, msg=name)
            self.assertAlmostEqual(c["height"], exp["height"], places=2, msg=name)
            if "font_px" in exp:
                got_px = calibrate.to_px(c["style"]["font-size"])
                self.assertAlmostEqual(got_px, exp["font_px"], places=2, msg=name)
            if "color" in exp:
                self.assertEqual(calibrate.parse_color(c["style"]["color"]), exp["color"], msg=name)

    def test_nested_offsets_accumulate(self):
        self._check(FIXTURES / "reference-basic.html", expect_scale=1.0)

    def test_scale_transform_is_detected_and_normalized(self):
        # Every raw pixel/pt value in this fixture is pre-multiplied by 1.25 and
        # wrapped in a transform:scale(1.25) ancestor -- after normalization it must
        # produce the EXACT SAME boxes as the unscaled fixture above.
        self._check(FIXTURES / "reference-scaled.html", expect_scale=1.25)


class AuthoredSizeTest(unittest.TestCase):
    """The renderer must read the app's authored canvas size from its .msapp
    (DocumentLayoutWidth/Height) rather than assume 1366x768, and render_screen() must use it
    as the default root size."""

    def test_authored_size_read_from_msapp(self):
        import tempfile, zipfile, shutil
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "app"
            (app / "Src").mkdir(parents=True)
            shutil.copy(FIXTURES / "basic.pa.yaml", app / "Src" / "basic.pa.yaml")
            with zipfile.ZipFile(Path(d) / "app.msapp", "w") as z:
                z.writestr("Properties.json", '{"DocumentLayoutWidth": 1600, "DocumentLayoutHeight": 900}')
                z.writestr("Src/basic.pa.yaml", (FIXTURES / "basic.pa.yaml").read_text(encoding="utf-8"))
            w, h, source = find_authored_size(app / "Src" / "basic.pa.yaml")
            self.assertEqual((w, h), (1600, 900))
            self.assertTrue(source.endswith(".msapp"))

    def test_authored_size_used_as_default_root(self):
        html_doc, _report = rs.render_screen(FIXTURES / "basic.pa.yaml")
        # tests/fixtures has no .msapp packaging basic.pa.yaml -> falls back to 1366x768
        self.assertIn("1366", html_doc)
        self.assertIn("768", html_doc)
        self.assertIn("1366x768 fallback", html_doc)

    def test_root_size_override_wins(self):
        html_doc, _report = rs.render_screen(FIXTURES / "basic.pa.yaml", root_size_override=(1698, 798))
        self.assertIn('width:1698px;height:798px', html_doc)
        self.assertIn("explicit root-size override", html_doc)


class ViewScaleTest(unittest.TestCase):
    """--view fit-1080p CSS-scales the emitted canvas to simulate a 1920x1080
    display without touching any authored-px layout math; default view is a no-op."""

    def test_native_view_has_no_scale_transform(self):
        html_doc, _report = rs.render_screen(FIXTURES / "basic.pa.yaml", root_size_override=(1366, 768))
        self.assertNotIn("transform:scale(", html_doc)

    def test_fit_1080p_applies_expected_scale(self):
        html_doc, _report = rs.render_screen(FIXTURES / "basic.pa.yaml", root_size_override=(1366, 768),
                                              view="fit-1080p")
        expected_scale = min(1920 / 1366, 1080 / 768)
        self.assertIn(f"transform:scale({expected_scale:.6f})", html_doc)
        self.assertIn("fit-1080p", html_doc)

    def test_compute_view_scale_is_width_or_height_constrained_correctly(self):
        # A very wide/short canvas should be height-constrained; a near-square one
        # width-constrained -- sanity-checks compute_view_scale directly.
        self.assertAlmostEqual(rs.compute_view_scale("fit-1080p", 1366, 768), min(1920 / 1366, 1080 / 768))
        self.assertEqual(rs.compute_view_scale("native", 1366, 768), 1.0)
        self.assertEqual(rs.compute_view_scale(None, 1366, 768), 1.0)



class ThemeConfigTest(unittest.TestCase):
    """Themes are data: the app's own theme record, an explicit --theme-file, else the neutral palette."""

    def test_neutral_palette_when_app_has_no_theme(self):
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(FIXTURES / "basic.pa.yaml", Path(d) / "basic.pa.yaml")   # no sibling App.pa.yaml
            html_doc, _r = rs.render_screen(Path(d) / "basic.pa.yaml")
            self.assertIn("theme-neutral.json", html_doc)

    def test_theme_file_overrides_app_theme(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "tokens.json"
            tf.write_text(json.dumps({"tokens": {"Accent": "#123456", "Text": "#654321", "Bg": "#000000"}}), encoding="utf-8")
            html_doc, _r = rs.render_screen(FIXTURES / "basic.pa.yaml", theme_file=str(tf))
            self.assertIn("tokens.json", html_doc)

    def test_extra_theme_var_name(self):
        cc.THEME_VARS.add("varPalette")
        try:
            ctx = cc.EvalContext(parent_w=100, parent_h=100, theme={"Accent": "#abcdef"}, vars={})
            v, _ = cc.eval_formula("=varPalette.Accent", ctx)
            self.assertEqual(v, "#abcdef")
        finally:
            cc.THEME_VARS.discard("varPalette")


if __name__ == "__main__":
    unittest.main(verbosity=1)
