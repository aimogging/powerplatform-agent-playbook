#!/usr/bin/env python3
"""
render-screen.py -- pa.yaml -> self-contained HTML LAYOUT/TYPOGRAPHY preview.

    python render-screen.py <screen.pa.yaml> [--sample-data samples.json] [--out out.html]

Renders one canvas screen source file to a single HTML file (inline CSS only, no
external assets besides the system "Segoe UI" font) that approximates what Studio
would show for layout review: absolute-positioned boxes, px-accurate X/Y/Width/Height,
Fill/Color/BorderColor/radius, Size -> font-size (pt*4/3 -> px), FontWeight, Align, Wrap.

This is a REVIEW tool, not a compiler: every property is resolved through a ladder
(explicit yaml formula -> defaults-catalog.json -> conservative hardcoded fallback),
and anything below "explicit" is reported at the bottom of the page. Fallback-tier
resolutions are additionally outlined with a dashed amber border on the canvas itself
(toggle with the "Show defaulted/unresolved" checkbox in the header). Controls whose
displayed value came from taking the first branch of an If()/Switch() are outlined
dashed purple ("Show dynamic controls" checkbox).

See capa_common.py for the formula evaluator, theme extraction, and catalog ladder --
nothing in this file should hardcode a control's default styling; that belongs in
defaults-catalog.json (see build-catalog.py) or, as an absolute last resort,
capa_common.FALLBACK_DEFAULTS.
"""
import argparse
import html as html_lib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capa_common import (  # noqa: E402
    EvalContext, UNRESOLVED, THEME_VARS, base_name, load_theme_file, catalog_lookup, control_key,
    eval_formula, find_authored_size, get_screen, is_known_control, iter_children,
    load_app_theme_for_screen, load_catalog, load_pa_yaml, FALLBACK_DEFAULTS,
)

FONT_WEIGHT_MAP = {"Bold": 700, "Semibold": 600, "Normal": 400, "Lighter": 300, "Light": 300}
ALIGN_MAP = {"Left": "left", "Center": "center", "Right": "right", "Justify": "justify"}
VALIGN_MAP = {"Top": "flex-start", "Middle": "center", "Bottom": "flex-end"}
JUSTIFY_MAP = {"left": "flex-start", "center": "center", "right": "flex-end", "justify": "flex-start"}


# --- ModernButton Appearance ------------------------------------------------
# The catalog's ModernButton defaults describe the PRIMARY appearance (dark
# #242424 fill, white text) because that is what an unqualified ModernButton
# renders as. Studio also ships Transparent / Subtle / Outline / Secondary
# appearances, which change the *chrome* -- and real apps lean on
# `Appearance: =ButtonAppearance.Transparent` for the invisible overlay buttons
# that make a gallery row or a tile tappable. Without this rule those overlays
# paint as opaque dark rectangles and swallow the whole design underneath them.
#
# The rule only supplies chrome the author did NOT write: an explicit Fill /
# BorderColor / BorderThickness in the yaml always wins.
BUTTON_APPEARANCE_CHROME = {
    # appearance -> (fill, border-from-palette, border thickness)
    "Transparent": ("transparent", False, 0),
    "Subtle": ("transparent", False, 0),
    "Outline": ("transparent", True, 1),
}


def apply_button_appearance(base, props, rp, fill, bc, bt):
    """Resolve a ModernButton's chrome from Appearance + BasePaletteColor.

    Two rules, both only supplying what the author did NOT write explicitly:
      * PRIMARY (no Appearance, or an appearance this table does not cover) takes
        its fill from `BasePaletteColor` when one is authored -- that is what the
        palette property is FOR, and without it every branded button renders as
        the catalog's neutral #242424.
      * TRANSPARENT / SUBTLE / OUTLINE drop the fill entirely (see the table).
    """
    if base != "ModernButton":
        return fill, bc, bt
    appearance = rp("Appearance") if "Appearance" in props else "Primary"
    chrome = BUTTON_APPEARANCE_CHROME.get(appearance)
    if chrome is None:
        if "Fill" not in props and "BasePaletteColor" in props:
            palette = rp("BasePaletteColor")
            if isinstance(palette, str) and palette:
                fill = palette
        return fill, bc, bt
    ap_fill, palette_border, ap_bt = chrome
    if "Fill" not in props:
        fill = ap_fill
    if "BorderThickness" not in props:
        bt = ap_bt
    if "BorderColor" not in props and bt > 0:
        bc = rp("BasePaletteColor") if palette_border and "BasePaletteColor" in props else bc
    if bt <= 0:
        bc = None
    return fill, bc, bt


def size_to_px(base, size):
    """Studio's `Size` property is a point value for classic/AppMagic-era text
    controls (Label, Classic/Button, Classic/TextInput) -- those set a real CSS
    `font-size: Npt`, and *browsers* convert pt->px at the standard 96dpi rate
    (1pt = 4/3px); that's where the Size*4/3 rule comes from, and it's a browser
    fact, not a Power Apps one.

    Calibration against a captured Studio DOM (see calibrate.py) showed the newer Fluent "Modern*"
    controls do NOT do this: a ModernButton/ModernTextInput/ModernDropdown/
    ModernDatePicker with Size=10.5 renders `font-size: 10.5px` directly -- no pt,
    no conversion. Confirmed empirically for ModernButton/ModernTextInput/
    ModernDropdown (all three appear with visible text in that capture); applied
    to ModernDatePicker by the same "Modern* = Fluent = px-native" reasoning,
    though no ModernDatePicker happened to be in the captured screen to confirm
    directly -- flag this specific one if a future calibration run disagrees.
    """
    return float(size) if base.startswith("Modern") else float(size) * 4 / 3

# Control types where Fill/BorderColor/BorderThickness/Radius are ALWAYS resolved
# (Studio always paints some background/border for these, even when the .pa.yaml
# omits every color property -- that omission is exactly the "defaults gotcha" this
# tool exists to surface).
ALWAYS_BOX = {
    "GroupContainer", "Rectangle", "Classic/Button", "ModernButton",
    "Classic/TextInput", "ModernTextInput", "ModernDropdown", "ModernDatePicker",
    "HtmlViewer", "Gallery",
}
# Control types where Radius is meaningful even when no border is drawn (rounded
# fill corners).
RADIUS_TYPES = {
    "Classic/Button", "ModernButton", "Classic/TextInput", "ModernTextInput",
    "ModernDropdown", "ModernDatePicker", "GroupContainer",
}


def esc(s):
    return html_lib.escape("" if s is None else str(s))


def num(v, default=0.0):
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return default


def color_css(v):
    if isinstance(v, str) and v:
        return v
    return "transparent"


class Report:
    def __init__(self):
        self.defaulted = []     # (ctrl_name, ctrl_key, prop, value, source, reason, matched_key)
        self.unresolved = []    # (ctrl_name, ctrl_key, prop, value, reason)
        self.unknown_controls = []  # (ctrl_name, ctrl_key)
        self.gallery_notes = []     # str
        self.missing_theme_tokens = set()
        self.text_unresolved = 0
        self.control_count = 0
        # name -> {key,x,y,w,h, [fill,border_color,border_thickness], [size_px,color,font_weight]}.
        # Populated live during rendering (not re-parsed out of the emitted HTML) so
        # calibrate.py has an exact, structured record of what we resolved per control.
        self.geometry = {}

    def counts(self):
        return {
            "defaulted": len(self.defaulted),
            "unresolved": len(self.unresolved),
            "unknown_controls": len(self.unknown_controls),
            "controls": self.control_count,
            "text_unresolved": self.text_unresolved,
        }


def resolve_property(prop, raw, ctrl_key, ctrl_name, ctx: EvalContext, catalog, report: Report):
    """explicit yaml -> defaults-catalog -> conservative flagged fallback."""
    if isinstance(raw, str):
        ctx.dynamic = False
        val, dynamic = eval_formula(raw, ctx)
        if val is not UNRESOLVED:
            return val, dynamic, False
        reason = "unresolved-expression"
    else:
        reason = "missing"
        dynamic = False

    cat_val, source, matched_key = catalog_lookup(catalog, ctrl_key, prop)
    if cat_val is not None:
        report.defaulted.append((ctrl_name, ctrl_key, prop, cat_val, source, reason, matched_key))
        return cat_val, dynamic, False

    fb = FALLBACK_DEFAULTS.get(prop)
    report.unresolved.append((ctrl_name, ctrl_key, prop, fb, reason))
    return fb, dynamic, True


def stringify(v):
    if v is UNRESOLVED or v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, str):
        return v
    return None


def resolve_text(raw, ctx: EvalContext):
    if not isinstance(raw, str):
        return None, False
    ctx.dynamic = False
    val, dynamic = eval_formula(raw, ctx)
    s = stringify(val)
    return s, dynamic


def wrap_div(name, key, x, y, w, h, inner, flagged, dynamic, extra_style="", overflow="hidden", extra_attrs=""):
    classes = ["ctrl"]
    if flagged:
        classes.append("flagged")
    if dynamic:
        classes.append("dynamic")
    style = f"left:{x:.1f}px;top:{y:.1f}px;width:{max(w,0):.1f}px;height:{max(h,0):.1f}px;overflow:{overflow};{extra_style}"
    title = f"{name}  ({key})  x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f}"
    return (f'<div class="{" ".join(classes)}" style="{style}" '
            f'data-name="{esc(name)}" data-key="{esc(key)}" title="{esc(title)}" {extra_attrs}>{inner}</div>')


def box_style(fill, border_color, border_thickness, radii):
    bt = num(border_thickness)
    border = f"border:{bt:.1f}px solid {color_css(border_color)};" if bt > 0 else "border:none;"
    tl, tr, br, bl = [num(r) for r in radii]
    radius = (f"border-top-left-radius:{tl:.0f}px;border-top-right-radius:{tr:.0f}px;"
              f"border-bottom-right-radius:{br:.0f}px;border-bottom-left-radius:{bl:.0f}px;")
    return f"background:{color_css(fill)};{border}{radius}box-sizing:border-box;"


def render_placeholder_unknown(name, key):
    return (f'<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;'
            f'flex-direction:column;background:repeating-linear-gradient(45deg,#f3d9a4,#f3d9a4 8px,#eec06a 8px,#eec06a 16px);'
            f'color:#5c4413;font:11px \'Segoe UI\',sans-serif;text-align:center;box-sizing:border-box;padding:2px;">'
            f'<div>UNSUPPORTED</div><div style="font-weight:600;">{esc(key)}</div><div>{esc(name)}</div></div>')


def render_placeholder_media(name, kind_label):
    icon = "\U0001F5BC" if kind_label == "Image" else "\U0001F4CE"
    return (f'<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;'
            f'flex-direction:column;background:#eceff1;color:#607d8b;font:11px \'Segoe UI\',sans-serif;'
            f'border:1px dashed #b0bec5;box-sizing:border-box;text-align:center;">'
            f'<div style="font-size:20px;">{icon}</div><div>{esc(kind_label)}</div><div>{esc(name)}</div></div>')


def render_node(name, node, parent_w, parent_h, this_item, sample_data, theme, var_env, catalog, report: Report,
                 parent_abs=(0.0, 0.0)):
    key = control_key(node)
    props = node.get("Properties") or {}
    report.control_count += 1

    ctx = EvalContext(parent_w=parent_w, parent_h=parent_h, this_item=this_item, theme=theme, vars=var_env)
    self_resolved = {}
    ctx.self_resolved = self_resolved
    ctrl_flags = {"flagged": False, "dynamic": False}

    def rp(prop):
        raw = props.get(prop)
        val, dynamic, flagged = resolve_property(prop, raw, key, name, ctx, catalog, report)
        if dynamic:
            ctrl_flags["dynamic"] = True
        if flagged:
            ctrl_flags["flagged"] = True
        self_resolved[prop] = val
        return val

    # Width/Height before X/Y: X/Y formulas commonly reference Self.Width/Self.Height
    # (e.g. "Parent.Width - Self.Width - 16"); the reverse is rare in practice.
    w = num(rp("Width"))
    h = num(rp("Height"))
    x = num(rp("X"))
    y = num(rp("Y"))

    base = base_name(key)
    abs_x, abs_y = parent_abs[0] + x, parent_abs[1] + y
    report.geometry[name] = {"key": key, "x": x, "y": y, "w": w, "h": h, "abs_x": abs_x, "abs_y": abs_y}

    if theme is not None:
        report.missing_theme_tokens |= ctx.missing_theme_tokens

    if not is_known_control(key):
        report.unknown_controls.append((name, key))
        ctrl_flags["flagged"] = True
        return wrap_div(name, key, x, y, w, h, render_placeholder_unknown(name, key), True, ctrl_flags["dynamic"])

    # --- box styling (Fill/BorderColor/BorderThickness/Radius) ------------------
    has_explicit_box = any(p in props for p in
                            ("Fill", "BorderColor", "BorderThickness",
                             "RadiusTopLeft", "RadiusTopRight", "RadiusBottomLeft", "RadiusBottomRight"))
    style = ""
    if base in ALWAYS_BOX or has_explicit_box:
        fill = rp("Fill")
        bt = num(rp("BorderThickness"))
        bc = rp("BorderColor") if bt > 0 else None
        fill, bc, bt = apply_button_appearance(base, props, rp, fill, bc, bt)
        if base in RADIUS_TYPES or has_explicit_box:
            radii = [rp("RadiusTopLeft"), rp("RadiusTopRight"), rp("RadiusBottomRight"), rp("RadiusBottomLeft")]
        else:
            radii = [0, 0, 0, 0]
        style = box_style(fill, bc, bt, radii)
        report.geometry[name].update({"fill": fill if isinstance(fill, str) else None,
                                       "border_color": bc if isinstance(bc, str) else None,
                                       "border_thickness": bt})

    # --- per-type content ---------------------------------------------------
    if base == "GroupContainer":
        inner = render_children(node.get("Children"), w, h, this_item, sample_data, theme, var_env, catalog, report,
                                 parent_abs=(abs_x, abs_y))
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)

    if base == "Rectangle":
        return wrap_div(name, key, x, y, w, h, "", ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)

    if base == "Image":
        return wrap_div(name, key, x, y, w, h, render_placeholder_media(name, "Image"),
                         ctrl_flags["flagged"], ctrl_flags["dynamic"])

    if base == "Attachments":
        return wrap_div(name, key, x, y, w, h, render_placeholder_media(name, "Attachments"),
                         ctrl_flags["flagged"], ctrl_flags["dynamic"])

    if base == "Label":
        return _render_text_like(name, key, props, ctx, rp, x, y, w, h, style, ctrl_flags, report,
                                  center=False, placeholder_prop=None)

    if base in ("Classic/Button", "ModernButton"):
        size = num(rp("Size"), 13)
        color = color_css(rp("Color"))
        fw = FONT_WEIGHT_MAP.get(rp("FontWeight"), 400)
        text_val, tdyn = resolve_text(props.get("Text"), ctx)
        if tdyn:
            ctrl_flags["dynamic"] = True
        label = esc(text_val) if text_val is not None else '<span class="unresolved-text">(dynamic label)</span>'
        px = size_to_px(base, size)
        inner_style = (f"display:flex;align-items:center;justify-content:center;width:100%;height:100%;"
                       f"color:{color};font-family:'Segoe UI',sans-serif;font-size:{px:.2f}px;"
                       f"font-weight:{fw};text-align:center;box-sizing:border-box;padding:0 {12 if base == 'ModernButton' else 4}px;"
                       f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis;")
        inner = f'<div style="{inner_style}">{label}</div>'
        report.geometry[name].update({"size_px": px, "color": color, "font_weight": fw})
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)

    if base in ("Classic/TextInput", "ModernTextInput"):
        size = num(rp("Size"), 14)
        color = color_css(rp("Color"))
        default_val, ddyn = resolve_text(props.get("Default"), ctx)
        placeholder_val, pdyn = resolve_text(props.get("Placeholder"), ctx)
        if ddyn or pdyn:
            ctrl_flags["dynamic"] = True
        # Single-line ellipsis: the truncation pattern (Label + Wrap=false,
        # ModernButton unconditionally) extended to ModernTextInput/Classic-TextInput --
        # min-width:0 lets the span shrink inside the flex row so overflow:hidden +
        # text-overflow:ellipsis actually bite instead of being ignored at content size.
        _ellipsis = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;min-width:0;"
        if default_val:
            text_html = f'<span style="{_ellipsis}">{esc(default_val)}</span>'
        elif placeholder_val:
            text_html = f'<span style="color:#8a8886;{_ellipsis}">{esc(placeholder_val)}</span>'
        else:
            text_html = ""
        px = size_to_px(base, size)
        inner_style = (f"display:flex;align-items:center;width:100%;height:100%;color:{color};"
                       f"font-family:'Segoe UI',sans-serif;font-size:{px:.2f}px;box-sizing:border-box;"
                       f"padding:0 10px;white-space:nowrap;overflow:hidden;")
        # Classic multiline text inputs are top-aligned wrapping editors, not
        # vertically centred single-line fields. ModernTextInput has no Mode.
        mode, mode_dynamic = eval_formula(props.get("Mode", "=TextMode.SingleLine"), ctx) if base == "Classic/TextInput" else ("SingleLine", False)
        if mode_dynamic:
            ctrl_flags["dynamic"] = True
        if mode in ("MultiLine", "TextMode.MultiLine"):
            padding = [num(rp(p), 0) for p in ("PaddingTop", "PaddingRight", "PaddingBottom", "PaddingLeft")]
            inner_style = (f"display:block;width:100%;height:100%;color:{color};"
                           f"font-family:'Segoe UI',sans-serif;font-size:{px:.2f}px;box-sizing:border-box;"
                           f"padding:{padding[0]}px {padding[1]}px {padding[2]}px {padding[3]}px;"
                           "white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.35;overflow:auto;")
        inner = f'<div style="{inner_style}">{text_html}</div>'
        report.geometry[name].update({"size_px": px, "color": color})
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)

    if base == "ModernDropdown":
        size = num(rp("Size"), 14)
        color = color_css(rp("Color"))
        px = size_to_px(base, size)
        # A dropdown seeded with Default shows that value, not the empty-state
        # placeholder -- Studio renders the selected item as soon as Default resolves.
        default_val, ddyn = resolve_text(props.get("Default"), ctx)
        if ddyn:
            ctrl_flags["dynamic"] = True
        _ellipsis = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;min-width:0;"
        if default_val:
            value_html = f'<span style="{_ellipsis}">{esc(default_val)}</span>'
        else:
            value_html = f'<span style="color:#8a8886;{_ellipsis}">Select an item</span>'
        inner_style = (f"display:flex;align-items:center;justify-content:space-between;width:100%;height:100%;"
                       f"color:{color};font-family:'Segoe UI',sans-serif;font-size:{px:.2f}px;"
                       f"box-sizing:border-box;padding:0 10px;white-space:nowrap;overflow:hidden;gap:6px;")
        inner = f'<div style="{inner_style}">{value_html}<span style="flex:0 0 auto;">▾</span></div>'
        report.geometry[name].update({"size_px": px, "color": color})
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)

    if base == "ModernDatePicker":
        size = num(rp("Size"), 14)
        color = color_css(rp("Color"))
        px = size_to_px(base, size)
        inner_style = (f"display:flex;align-items:center;justify-content:space-between;width:100%;height:100%;"
                       f"color:{color};font-family:'Segoe UI',sans-serif;font-size:{px:.2f}px;"
                       f"box-sizing:border-box;padding:0 10px;white-space:nowrap;overflow:hidden;")
        inner = f'<div style="{inner_style}"><span style="color:#8a8886;">mm/dd/yyyy</span><span>\U0001F4C5</span></div>'
        report.geometry[name].update({"size_px": px, "color": color})
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)

    if base == "HtmlViewer":
        html_val, hdyn = resolve_text(props.get("HtmlText"), ctx)
        if hdyn:
            ctrl_flags["dynamic"] = True
        if html_val is not None:
            body = html_val
        else:
            body = ('<div style="font:italic 11px \'Segoe UI\',sans-serif;color:#8a8886;padding:8px;">'
                    '(HTML content computed at runtime -- formula not statically resolvable)</div>')
            report.text_unresolved += 1
        inner = f'<div style="width:100%;height:100%;overflow:auto;box-sizing:border-box;">{body}</div>'
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"],
                         extra_style=style, overflow="hidden")

    if base == "Gallery":
        template_size = num(rp("TemplateSize"), 100)
        template_padding = num(rp("TemplatePadding"), 0)
        variant = node.get("Variant", "Vertical")
        vertical = variant != "Horizontal"

        rows = None
        if isinstance(this_item, dict) and isinstance(this_item.get(name), list):
            rows = this_item.get(name)
        elif isinstance(sample_data.get(name), list):
            rows = sample_data.get(name)
        if not rows:
            report.gallery_notes.append(
                f"Gallery '{name}' ({key}): no sample data found under this name -- rendering 1 placeholder row.")
            rows = [{}]

        row_html_parts = []
        for i, row in enumerate(rows):
            if vertical:
                row_w, row_h = w, template_size
                row_x, row_y = 0, i * (template_size + template_padding)
            else:
                row_w, row_h = template_size, h
                row_x, row_y = i * (template_size + template_padding), 0
            row_inner = render_children(node.get("Children"), row_w, row_h, row, sample_data, theme, var_env, catalog,
                                         report, parent_abs=(abs_x + row_x, abs_y + row_y))
            row_html_parts.append(
                f'<div class="ctrl" style="left:{row_x:.1f}px;top:{row_y:.1f}px;width:{row_w:.1f}px;'
                f'height:{row_h:.1f}px;overflow:hidden;" data-name="{esc(name)}-row{i}">{row_inner}</div>')
        inner = "".join(row_html_parts)
        overflow = "auto"
        return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"],
                         extra_style=style, overflow=overflow)

    # Should be unreachable given is_known_control(), but stay safe.
    return wrap_div(name, key, x, y, w, h, render_placeholder_unknown(name, key), True, ctrl_flags["dynamic"])


def _render_text_like(name, key, props, ctx, rp, x, y, w, h, style, ctrl_flags, report, center, placeholder_prop):
    size = num(rp("Size"), 13)
    color = color_css(rp("Color"))
    fw = FONT_WEIGHT_MAP.get(rp("FontWeight"), 400)
    align = ALIGN_MAP.get(rp("Align"), "left")
    valign = VALIGN_MAP.get(rp("VerticalAlign"), "flex-start")
    wrap_flag = rp("Wrap")
    font_raw = props.get("Font")
    font_name = None
    if isinstance(font_raw, str):
        fv, _ = eval_formula(font_raw, ctx)
        if isinstance(fv, str):
            font_name = fv
    font_family = f"'{font_name}', 'Segoe UI', sans-serif" if font_name else "'Segoe UI', sans-serif"

    text_val, tdyn = resolve_text(props.get("Text"), ctx)
    if tdyn:
        ctrl_flags["dynamic"] = True
    if text_val is not None:
        display = esc(text_val).replace("\n", "<br>")
    else:
        display = '<span class="unresolved-text">(dynamic text)</span>'
        report.text_unresolved += 1

    px = size * 4 / 3
    wrap_css = "normal" if wrap_flag not in (False, "false") else "nowrap"
    overflow_css = "visible" if wrap_css == "normal" else "hidden"
    text_overflow = "" if wrap_css == "normal" else "text-overflow:ellipsis;"
    inner_style = (f"width:100%;color:{color};font-family:{font_family};font-size:{px:.2f}px;"
                   f"font-weight:{fw};text-align:{align};white-space:{wrap_css};{text_overflow}"
                   f"overflow:{overflow_css};word-break:break-word;")
    box_flex = f"display:flex;align-items:{valign};justify-content:{JUSTIFY_MAP.get(align,'flex-start')};"
    inner = f'<div style="{box_flex}width:100%;height:100%;box-sizing:border-box;padding:1px 3px;"><div style="{inner_style}">{display}</div></div>'
    report.geometry[name].update({"size_px": px, "color": color, "font_weight": fw})
    return wrap_div(name, key, x, y, w, h, inner, ctrl_flags["flagged"], ctrl_flags["dynamic"], extra_style=style)


def nested_visible_false(props, parent_w, parent_h, this_item, theme, var_env):
    """True when a NESTED control (a container's child, or a gallery row template's
    child) is provably invisible -- its Visible formula resolves to a concrete False
    against the seeded vars and, in a gallery, this row's own ThisItem values.

    TOP-LEVEL screen children deliberately paint regardless of Visible: the layer
    panel owns those, so a reviewer can flip a modal or a scrim on. Nested children
    are different, and painting them all is actively misleading:
      - the flattened-grouping technique puts a band-header template and a task-row
        template in the SAME gallery, mutually exclusive on `ThisItem.rowType`;
      - a detail panel swaps whole sections in and out on one variable.
    Painting both sides of either swap buries the layout under itself. Only a
    statically-False Visible hides anything; UNRESOLVED still paints, so nothing
    genuinely runtime-dependent silently disappears."""
    raw = props.get("Visible")
    if not isinstance(raw, str):
        return False
    ctx = EvalContext(parent_w=parent_w, parent_h=parent_h, this_item=this_item,
                      theme=theme, vars=var_env)
    val, _dynamic = eval_formula(raw, ctx)
    return val is False


def render_children(children, parent_w, parent_h, this_item, sample_data, theme, var_env, catalog, report,
                     parent_abs=(0.0, 0.0)):
    """Renders a NESTED child list. The screen root does not come through here --
    render_screen() walks top-level children itself so the layer panel can own them."""
    parts = []
    for name, node in iter_children(children):
        if nested_visible_false(node.get("Properties") or {}, parent_w, parent_h, this_item,
                                 theme, var_env):
            continue
        parts.append(render_node(name, node, parent_w, parent_h, this_item, sample_data, theme, var_env, catalog,
                                  report, parent_abs=parent_abs))
    return "".join(parts)


def resolve_initial_visible(props, parent_w, parent_h, theme, var_env):
    """Best-effort, non-flagging peek at a control's Visible formula, used only to
    decide the LAYER PANEL's initial checkbox state / display:none. Returns
    (visible_or_None, raw_formula_or_None) -- None for visible means "not statically
    resolvable", which defaults to shown (checked), matching Studio's own default of
    Visible=true when unspecified/unknown."""
    raw = props.get("Visible")
    if not isinstance(raw, str):
        return True, None
    ctx = EvalContext(parent_w=parent_w, parent_h=parent_h, theme=theme, vars=var_env)
    val, _dynamic = eval_formula(raw, ctx)
    if val is True:
        return True, raw
    if val is False:
        return False, raw
    return None, raw


# ---------------------------------------------------------------------------
# HTML document assembly
# ---------------------------------------------------------------------------

PAGE_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Segoe UI', Tahoma, Arial, sans-serif; background: #f3f3f3; color: #202020; }
#header { background: #1c1f26; color: #eee; padding: 10px 16px; display: flex; align-items: center; gap: 20px; flex-wrap: wrap; position: sticky; top: 0; z-index: 1000; }
#header h1 { font-size: 15px; margin: 0; font-weight: 600; }
#header .meta { font-size: 12px; color: #b6b9c2; }
#header label { font-size: 12px; display: flex; align-items: center; gap: 5px; cursor: pointer; }
#stage-wrap { display: flex; align-items: flex-start; }
#layer-panel { flex: 0 0 300px; width: 300px; max-height: calc(100vh - 53px); overflow: auto; position: sticky;
  top: 53px; background: #fff; border-right: 1px solid #ccc; padding: 12px; font-size: 12px; box-sizing: border-box; }
#layer-panel h2 { font-size: 13px; margin: 0 0 4px; }
#layer-panel .hint { color: #777; font-size: 11px; margin: 0 0 10px; }
#layer-panel label { display: flex; gap: 7px; align-items: flex-start; padding: 6px 0; border-bottom: 1px solid #eee; cursor: pointer; }
#layer-panel input[type=checkbox] { margin-top: 2px; flex: 0 0 auto; }
#layer-panel .layer-name { font-weight: 600; }
#layer-panel .layer-key { color: #888; font-weight: 400; }
#layer-panel .layer-reason { display: block; color: #999; font-size: 11px; margin-top: 2px; }
#layer-panel .layer-reason.hidden-by-seed { color: #b45309; }
#layer-panel .layer-members { display: block; color: #aaa; font-size: 10px; margin-top: 2px; font-style: italic; }
#canvas-area { flex: 1 1 auto; padding: 24px; overflow: auto; }
#canvas { position: relative; box-shadow: 0 0 0 1px #ccc, 0 4px 16px rgba(0,0,0,0.15); margin: 0 auto; }
.ctrl { position: absolute; }
body:not(.show-flags) .flagged { outline: none !important; }
body.show-flags .flagged { outline: 2px dashed #d97706; outline-offset: -2px; }
body:not(.show-dynamic) .dynamic { outline: none !important; }
body.show-dynamic .dynamic { outline: 2px dashed #7c3aed; outline-offset: -2px; }
body.show-flags .flagged.dynamic { outline: 2px dashed #d97706; box-shadow: inset 0 0 0 2px #7c3aed; }
.unresolved-text { color: #9aa0ad; font-style: italic; }
#report { max-width: 1366px; margin: 0 auto 60px; padding: 0 24px; }
#report h2 { font-size: 16px; border-bottom: 2px solid #ccc; padding-bottom: 6px; }
#report h3 { font-size: 13px; margin-top: 24px; }
#report table { border-collapse: collapse; width: 100%; font-size: 12px; background: #fff; }
#report th, #report td { border: 1px solid #ddd; padding: 4px 8px; text-align: left; }
#report th { background: #eee; }
#report .src-theme { color: #1f7a43; }
#report .src-documented { color: #4750e0; }
#report .src-inferred { color: #9a6400; }
#report .src-fallback, #report .empty-none { color: #c4453a; font-weight: 600; }
#report .counts { display: flex; gap: 24px; font-size: 13px; margin: 10px 0 20px; flex-wrap: wrap; }
#report .counts b { font-size: 20px; display: block; }
#report .note { font-size: 12px; color: #666; }
"""

PAGE_JS = """
function toggle(id, cls) {
  document.body.classList.toggle(cls, document.getElementById(id).checked);
}
window.__layerZTop = 500;
function toggleLayerGroup(cb) {
  var raw = cb.getAttribute('data-layer-group') || '';
  var names = raw.length ? raw.split(',') : [];
  var show = cb.checked;
  names.forEach(function(name) {
    var el = document.querySelector('[data-layer="' + CSS.escape(name) + '"]');
    if (!el) { return; }
    el.style.display = show ? '' : 'none';
    // Bring the group being turned on to the FRONT of the z-order: top-level
    // siblings paint in DOM/declaration order with no z-index, so an earlier
    // control that's toggled on can otherwise still be silently hidden behind a
    // later, already-visible full-screen overlay (e.g. another open modal) --
    // this is the actual cause of "the checkbox doesn't seem to do anything".
    // Bump only the direct positioned child (.ctrl); it and its whole subtree
    // move above every other top-level layer as one stacking-context unit.
    if (show && el.firstElementChild) {
      window.__layerZTop += 1;
      el.firstElementChild.style.zIndex = String(window.__layerZTop);
    }
  });
}
"""


# Top-level control names follow "<lowercase type prefix><PascalCase suffix>"
# (scrimWsConfirm, conWsConfirm, btnWsDismissResult, ...). The layer panel clusters
# related controls by stripping that prefix and matching on the remaining name's
# first+last CamelCase token -- e.g. scrimWsConfirm/conWsConfirm both reduce to
# ("Ws","Confirm"); lblWsResult ("Ws","Result") and btnWsDismissResult ("Ws","Result")
# match too even though the full remaining strings differ (WsResult vs
# WsDismissResult) -- "Dismiss" is a qualifier on the button's action, not the
# concept the group is about.
LAYER_TYPE_PREFIXES = ("scrim", "con", "btn", "lbl", "gal", "txt", "dd", "dp", "html", "rec")
_CAMEL_TOKEN_RE = re.compile(r"[A-Z][a-z0-9]*|[A-Z]+(?![a-z])")


def strip_layer_type_prefix(name):
    """Strip a leading lowercase type prefix (scrim/con/btn/...) if the next
    character starts a new CamelCase word -- so 'container' or 'button' (a real
    word, not a type tag) is never mistaken for the prefix."""
    for pfx in sorted(LAYER_TYPE_PREFIXES, key=len, reverse=True):
        if name.startswith(pfx) and len(name) > len(pfx) and name[len(pfx)].isupper():
            return pfx, name[len(pfx):]
    return None, name


def camel_tokens(s):
    return _CAMEL_TOKEN_RE.findall(s) or ([s] if s else [])


def layer_cluster_key(name):
    """The (first, last) CamelCase token of the name's suffix (after stripping its
    type prefix) -- two controls cluster together iff this key matches."""
    _prefix, remaining = strip_layer_type_prefix(name)
    toks = camel_tokens(remaining)
    if not toks:
        return (name, name)
    return (toks[0], toks[-1])


def prettify_group_label(remaining_suffix):
    """'WsTplMgr' -> 'Tpl Mgr'; 'WsConfirm' -> 'Confirm' (drop a leading 'Ws' app
    prefix token, space out the rest)."""
    toks = camel_tokens(remaining_suffix)
    if len(toks) > 1 and toks[0] == "Ws":
        toks = toks[1:]
    return " ".join(toks) if toks else remaining_suffix


def build_layer_group(members):
    """members: list of per-control layer dicts (see render_screen) that share a
    cluster key, in DOM order. Returns one panel-entry dict: {label, checked,
    member_names, reason_html}."""
    names = [m["name"] for m in members]
    # Canonical label comes from whichever member has the FEWEST remaining tokens
    # after stripping its prefix -- e.g. lblWsResult ("Ws","Result", 2 tokens) over
    # btnWsDismissResult ("Ws","Dismiss","Result", 3 tokens) -- so the group reads
    # "Result", not "Dismiss Result".
    remainders = [strip_layer_type_prefix(m["name"])[1] for m in members]
    canonical = min(remainders, key=lambda s: len(camel_tokens(s)))
    label = prettify_group_label(canonical)
    # A single GroupContainer with no scrim/sibling partner (e.g. a slide-in detail
    # panel) reads oddly as a bare noun -- call it out as a panel.
    if len(members) == 1 and strip_layer_type_prefix(members[0]["name"])[0] == "con":
        label = f"{label} Panel"
    checked = any(m["checked"] for m in members)
    raws = [m["raw"] for m in members]
    if all(r is None for r in raws):
        reason_html = "(no Visible property -- always shown)"
    elif len(set(raws)) == 1 and raws[0] is not None:
        reason_html = f"Visible: {esc(raws[0])} &rarr; {esc(members[0]['status'])}"
    else:
        parts = [f"{esc(m['name'])}: " + ("always shown" if m["raw"] is None else esc(m["status"]))
                 for m in members]
        reason_html = "; ".join(parts)
    return {"label": label, "checked": checked, "member_names": names, "reason_html": reason_html}


def build_base_screen_group(members):
    names = [m["name"] for m in members]
    checked = any(m["checked"] for m in members)
    reason_html = f"Base screen -- {len(members)} controls visible by default (toolbar, tiles, list, filters)"
    return {"label": "Base screen", "checked": checked, "member_names": names, "reason_html": reason_html}


def group_layers(layer_entries):
    """layer_entries: per-control dicts in DOM order (see render_screen), each with
    an extra 'default_shown' bool computed against the OnStart-seeded vars BEFORE any
    --set override, so the panel's own structure doesn't change just because a --set
    flag is previewing a different state. Clusters by layer_cluster_key(); any
    cluster whose every member is default_shown collapses into one always-on "Base
    screen" entry, everything else becomes its own named group -- so the panel ends
    up with roughly Base screen + one entry per overlay/modal cluster."""
    clusters = {}
    order = []
    for entry in layer_entries:
        ck = layer_cluster_key(entry["name"])
        if ck not in clusters:
            clusters[ck] = []
            order.append(ck)
        clusters[ck].append(entry)

    base_members = []
    groups = []
    for ck in order:
        members = clusters[ck]
        if all(m["default_shown"] for m in members):
            base_members.extend(members)
        else:
            groups.append(build_layer_group(members))

    result = []
    if base_members:
        result.append(build_base_screen_group(base_members))
    result.extend(groups)
    return result


def build_layer_panel_html(groups):
    """groups: list of dicts from group_layers() -- {label, checked, member_names,
    reason_html}. Renders the sidebar list of grouped checkboxes; each one drives
    toggleLayerGroup() in PAGE_JS via its data-layer-group attribute (a comma-joined
    list of the underlying data-layer names it controls)."""
    rows = []
    for g in groups:
        reason_cls = "layer-reason" + ("" if g["checked"] else " hidden-by-seed")
        member_names = g["member_names"]
        title_attr = esc(", ".join(member_names))
        shown = member_names[:6]
        members_line = ", ".join(shown)
        if len(member_names) > 6:
            members_line += f", +{len(member_names) - 6} more"
        data_group = esc(",".join(member_names))
        n = len(member_names)
        rows.append(
            f'<label title="{title_attr}">'
            f'<input type="checkbox" {"checked" if g["checked"] else ""} '
            f'data-layer-group="{data_group}" onchange="toggleLayerGroup(this)">'
            f'<span><span class="layer-name">{esc(g["label"])}</span> '
            f'<span class="layer-key">({n} control{"s" if n != 1 else ""})</span>'
            f'<span class="{reason_cls}">{g["reason_html"]}</span>'
            f'<span class="layer-members">{esc(members_line)}</span></span>'
            f'</label>'
        )
    return (f'<h2>Layers ({len(groups)} group{"s" if len(groups) != 1 else ""})</h2>'
            f'<p class="hint">Related top-level controls (scrim+panel modals, tile pairs, ...) are grouped under '
            f'one checkbox -- hover a row or see the dim sub-line for the underlying control names. Checkbox state '
            f'reflects the resolved Visible value of the group\'s members (OnStart-seeded vars + any --set '
            f'overrides). Checking a group brings it to the front, so it stays visible even if another overlay is '
            f'already open.</p>'
            + "".join(rows))


def build_html_document(screen_name, screen_path, root_w, root_h, screen_fill, children_html,
                         report: Report, theme_name, catalog_size, layer_panel_html,
                         root_size_source="authored", view="native", view_scale=1.0):
    counts = report.counts()

    def source_class(src):
        base = src.split(":")[0]
        return f"src-{base}" if base in ("theme", "documented", "inferred") else "empty-none"

    defaulted_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{esc(k)}</td><td>{esc(p)}</td><td>{esc(v)}</td>"
        f"<td class='{source_class(src)}'>{esc(src)}</td><td>{esc(reason)}</td></tr>"
        for (n, k, p, v, src, reason, matched_key) in report.defaulted
    ) or "<tr><td colspan='6'><i>none</i></td></tr>"

    unresolved_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{esc(k)}</td><td>{esc(p)}</td><td>{esc(v)}</td><td>{esc(reason)}</td></tr>"
        for (n, k, p, v, reason) in report.unresolved
    ) or "<tr><td colspan='5'><i>none</i></td></tr>"

    unknown_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{esc(k)}</td></tr>" for (n, k) in report.unknown_controls
    ) or "<tr><td colspan='2'><i>none</i></td></tr>"

    gallery_notes = "".join(f"<li>{esc(n)}</li>" for n in report.gallery_notes) or "<li><i>none</i></li>"
    missing_theme = ", ".join(sorted(report.missing_theme_tokens)) or "none"

    theme_note = (f"Active theme: <code>{esc(theme_name)}</code>" if theme_name
                  else "No App.pa.yaml theme found next to this screen -- varTheme.* references are unresolved.")

    size_source_labels = {
        "authored": "authored size (DocumentLayoutWidth/Height from the app's .msapp)",
        "screen-property": "screen's own explicit Width/Height property",
        "override": "explicit root-size override (e.g. calibrate.py matching a reference capture)",
        "fallback-default": "1366x768 fallback (no .msapp packaging this screen was found)",
    }
    size_note = size_source_labels.get(root_size_source, root_size_source)

    view_note = ""
    if view != "native" and view_scale != 1.0:
        eff_w, eff_h = root_w * view_scale, root_h * view_scale
        view_note = (f" &middot; view: <code>{esc(view)}</code> (scale {view_scale:.4f}x, "
                     f"simulated footprint {eff_w:.0f}&times;{eff_h:.0f} on a 1920&times;1080 display)")

    if view_scale != 1.0:
        canvas_wrap_style = (f"width:{root_w*view_scale:.0f}px;height:{root_h*view_scale:.0f}px;")
        canvas_style = (f"width:{root_w:.0f}px;height:{root_h:.0f}px;background:{color_css(screen_fill)};"
                        f"overflow:hidden;transform:scale({view_scale:.6f});transform-origin:top left;")
        canvas_block = (f'<div id="canvas-viewport" style="{canvas_wrap_style}">'
                         f'<div id="canvas" style="{canvas_style}">{children_html}</div></div>')
    else:
        canvas_style = f"width:{root_w:.0f}px;height:{root_h:.0f}px;background:{color_css(screen_fill)};overflow:hidden;"
        canvas_block = f'<div id="canvas" style="{canvas_style}">{children_html}</div>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{esc(screen_name)} -- canvas-design preview</title>
<style>{PAGE_CSS}</style>
</head>
<body class="show-flags show-dynamic">
<div id="header">
  <h1>{esc(screen_name)}</h1>
  <div class="meta">{esc(Path(screen_path).name)} &middot; {root_w:.0f}&times;{root_h:.0f} ({esc(size_note)}) &middot; {theme_note} &middot; catalog: {catalog_size} control@version entries{view_note}</div>
  <label><input type="checkbox" id="chkFlags" checked onchange="toggle('chkFlags','show-flags')"> Show defaulted/unresolved (amber)</label>
  <label><input type="checkbox" id="chkDynamic" checked onchange="toggle('chkDynamic','show-dynamic')"> Show dynamic If/Switch (purple)</label>
</div>
<div id="stage-wrap">
  <div id="layer-panel">{layer_panel_html}</div>
  <div id="canvas-area">
    {canvas_block}
  </div>
</div>
<div id="report">
  <h2>Defaulted / unresolved report</h2>
  <div class="counts">
    <div><b>{counts['controls']}</b>controls rendered</div>
    <div><b>{counts['defaulted']}</b>resolved from catalog (silent)</div>
    <div><b>{counts['unresolved']}</b>fallback used (flagged amber)</div>
    <div><b>{counts['unknown_controls']}</b>unsupported control types (flagged amber)</div>
    <div><b>{counts['text_unresolved']}</b>text/HTML bodies not statically resolvable</div>
  </div>
  <p class="note">Missing varTheme tokens referenced but not found in the active theme: {esc(missing_theme)}</p>

  <h3>Defaulted from catalog -- property omitted from the .pa.yaml, filled from defaults-catalog.json (NOT visually flagged; this is a silent, presumed-correct Studio default)</h3>
  <table><tr><th>Control</th><th>Control@Version</th><th>Property</th><th>Value used</th><th>Catalog source</th><th>Reason</th></tr>
  {defaulted_rows}</table>

  <h3>Unresolved -- catalog had no entry either; a conservative hardcoded fallback was used (VISIBLY flagged, dashed amber outline on canvas)</h3>
  <table><tr><th>Control</th><th>Control@Version</th><th>Property</th><th>Fallback value</th><th>Reason</th></tr>
  {unresolved_rows}</table>

  <h3>Unsupported control types (gray/amber hatch placeholder on canvas)</h3>
  <table><tr><th>Control</th><th>Control@Version</th></tr>
  {unknown_rows}</table>

  <h3>Gallery sample-data notes</h3>
  <ul>{gallery_notes}</ul>
</div>
<script>{PAGE_JS}</script>
</body></html>
"""


VISIBLE_STATUS_LABELS = {
    True: "resolved TRUE",
    False: "resolved FALSE",
    None: "not statically resolvable -- shown by default",
}


TARGET_1080P = (1920, 1080)


def compute_view_scale(view, root_w, root_h):
    """view: None/'native' -> no scaling (1.0). 'fit-1080p' -> the uniform scale
    factor that fits the authored root_w x root_h canvas inside the team's ideal
    display (1920x1080) without changing the authored-px layout math -- purely a
    CSS transform on the rendered canvas, applied AFTER every position/size in this
    file has already been computed in authored px."""
    if view in (None, "native"):
        return 1.0
    if view == "fit-1080p":
        tw, th = TARGET_1080P
        return min(tw / root_w, th / root_h)
    raise ValueError(f"unknown --view {view!r}")


def render_screen(screen_path, sample_data_path=None, out_path=None, catalog_path=None, var_overrides=None,
                   root_size_override=None, view="native", theme_override=None, theme_file=None):
    """theme_override: "varThemeLight" / "varThemeDark" (or any Set(varThemeXxx,{...})
    record name found in the sibling App.pa.yaml) to render this screen in a SPECIFIC
    theme regardless of App.OnStart's own Set(varDark, ...) literal. Needed because
    var_overrides (--set varDark=true) only feeds live formula evaluation (ctx.vars) --
    the active *palette* (ctx.theme) is resolved once, statically, from App.pa.yaml's
    own OnStart text, and var_overrides never touched it. Without this, a "-dark" mock
    state silently renders in light colours.

    theme_file: a token JSON (see theme-neutral.json). Given explicitly it wins over the app's own
    theme record; when the app defines no theme record at all, the neutral palette is used."""
    screen_path = Path(screen_path)
    doc = load_pa_yaml(screen_path)
    screen_name, screen_node = get_screen(doc)
    theme, theme_name, var_seeds, _app_doc, all_theme_records = load_app_theme_for_screen(screen_path)
    if theme_override and theme_override in all_theme_records:
        theme = all_theme_records[theme_override]
        theme_name = theme_override
    if theme_file:
        theme, theme_name = load_theme_file(theme_file), Path(theme_file).name
    elif not theme:
        theme, theme_name = load_theme_file(None), "theme-neutral.json (no theme record in App.pa.yaml)"
    catalog = load_catalog(catalog_path)
    report = Report()

    # OnStart-seeded vars first, then any explicit --set overrides win (lets a user
    # deliberately preview a state OnStart doesn't start in, e.g. a confirm modal).
    var_env = dict(var_seeds)
    if var_overrides:
        var_env.update(var_overrides)

    sample_data = {}
    if sample_data_path:
        sample_data = json.loads(Path(sample_data_path).read_text(encoding="utf-8"))

    # Root canvas size, in priority order: an explicit override (calibrate.py uses
    # this to match a reference capture's own live viewport size -- see
    # calibrate.py's docstring for why that's the right thing to
    # match against, not the authored size) > the screen's own explicit Width/Height
    # formula (rare, but a screen can set one) > the app's AUTHORED design size read
    # from its packed .msapp (DocumentLayoutWidth/Height -- never assumed) > the
    # hardcoded 1366x768 last resort when no .msapp packaging this screen was found.
    authored_w, authored_h, authored_source = find_authored_size(screen_path)
    root_w, root_h = authored_w, authored_h
    root_size_source = "authored" if authored_source else "fallback-default"

    screen_props = screen_node.get("Properties") or {}
    probe_ctx = EvalContext(parent_w=root_w, parent_h=root_h, theme=theme, vars=var_env)
    if isinstance(screen_props.get("Width"), str):
        v, _ = eval_formula(screen_props["Width"], probe_ctx)
        if isinstance(v, (int, float)):
            root_w = v
            root_size_source = "screen-property"
    if isinstance(screen_props.get("Height"), str):
        v, _ = eval_formula(screen_props["Height"], probe_ctx)
        if isinstance(v, (int, float)):
            root_h = v
            root_size_source = "screen-property"

    if root_size_override:
        root_w, root_h = root_size_override
        root_size_source = "override"

    fill_ctx = EvalContext(parent_w=root_w, parent_h=root_h, theme=theme, vars=var_env)
    screen_fill = "#ffffff"
    if isinstance(screen_props.get("Fill"), str):
        v, _ = eval_formula(screen_props["Fill"], fill_ctx)
        if isinstance(v, str):
            screen_fill = v

    # Render every top-level child (paint everything -- no suppression), but wrap each
    # one in a `data-layer` div so the LAYER PANEL can show/hide it live without a
    # re-render, and seed that div's initial display from the resolved Visible value
    # (OnStart var seeding + --set overrides) so the default view already looks like
    # the real screen: scrims/modals start hidden, the matrix/gallery starts shown.
    #
    # default_var_env (OnStart seeds only, no --set) is used ONLY to decide which
    # controls are "default_shown" for the Base-screen collapse in group_layers() --
    # rendering with --set varWsRollupOpen=true to preview a modal must not itself
    # reshuffle the panel's own grouping.
    default_var_env = dict(var_seeds)
    layers = []
    wrapped_children = []
    for name, node in iter_children(screen_node.get("Children")):
        props = node.get("Properties") or {}
        visible, raw = resolve_initial_visible(props, root_w, root_h, theme, var_env)
        checked = visible is not False
        default_visible, _default_raw = resolve_initial_visible(props, root_w, root_h, theme, default_var_env)
        layers.append({
            "name": name, "key": control_key(node), "checked": checked, "raw": raw,
            "status": VISIBLE_STATUS_LABELS[visible], "default_shown": default_visible is not False,
        })
        child_html = render_node(name, node, root_w, root_h, None, sample_data, theme, var_env, catalog, report)
        display = "" if checked else "display:none;"
        wrapped_children.append(f'<div data-layer="{esc(name)}" style="{display}">{child_html}</div>')
    children_html = "".join(wrapped_children)

    layer_panel_html = build_layer_panel_html(group_layers(layers))
    view_scale = compute_view_scale(view, root_w, root_h)

    doc_html = build_html_document(screen_name, screen_path, root_w, root_h, screen_fill, children_html,
                                    report, theme_name, len(catalog), layer_panel_html,
                                    root_size_source=root_size_source, view=view, view_scale=view_scale)

    if out_path:
        Path(out_path).write_text(doc_html, encoding="utf-8")

    return doc_html, report


def coerce_cli_value(s):
    """--set NAME=VALUE value coercion: true/false -> bool, numeric -> int/float,
    else left as a string. Mirrors capa_common's OnStart literal parsing."""
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def parse_set_args(pairs):
    overrides = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--set expects NAME=VALUE, got: {p!r}")
        name, value = p.split("=", 1)
        overrides[name.strip()] = coerce_cli_value(value.strip())
    return overrides


def to_png(html_path, png_path):
    """Screenshot a rendered HTML file (mock review at design gates). Optional dependency: Playwright."""
    from playwright.sync_api import sync_playwright
    png_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(Path(html_path).resolve().as_uri())
        page.screenshot(path=str(png_path), full_page=True)
        b.close()
    print(f"  png: {png_path}")


def main():
    if "--self-test" in sys.argv[1:]:
        import subprocess
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "tests" / "test_render.py")])
        print("self-test: " + ("PASS" if r.returncode == 0 else "FAIL"))
        return r.returncode
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("screen", help="path to a screen .pa.yaml file")
    ap.add_argument("--sample-data", help="JSON file: {galleryControlName: [rowDict, ...], ...}")
    ap.add_argument("--out", help="output .html path (default: <screen-basename>.html next to this script)")
    ap.add_argument("--catalog", help="override defaults-catalog.json path")
    ap.add_argument("--set", action="append", metavar="NAME=VALUE",
                     help="override an OnStart-seeded var for this render (repeatable), e.g. "
                          "--set varWsRollupOpen=true to preview a modal/scrim that starts hidden by default")
    ap.add_argument("--view", choices=["native", "fit-1080p"], default="native",
                     help="'native' (default): authored-px 1:1, no scaling. 'fit-1080p': CSS-scale the emitted "
                          "canvas to simulate how it looks fit to a 1920x1080 display (the team's ideal target) "
                          "without changing any authored-px layout math.")
    ap.add_argument("--root-size", metavar="WxH",
                     help="override the canvas root size instead of reading the authored DocumentLayoutWidth/"
                          "Height from the app's .msapp (mainly for calibrate.py, to render at a reference "
                          "capture's own measured viewport size)")
    ap.add_argument("--theme", metavar="varThemeXxx",
                     help="render in a SPECIFIC theme record from the sibling App.pa.yaml (e.g. varThemeDark), "
                          "overriding its own Set(varDark, ...) literal -- --set varDark=true alone only affects "
                          "live formula evaluation, not the palette (see render_screen's theme_override docstring)")
    ap.add_argument("--theme-file", help="token JSON to render with (default: the app's own theme record, else theme-neutral.json)")
    ap.add_argument("--theme-var", action="append", default=[], help="another record variable that holds theme tokens (default varTheme)")
    ap.add_argument("--png", help="also screenshot the render to this PNG (needs Playwright)")
    args = ap.parse_args()
    THEME_VARS.update(args.theme_var)

    screen_path = Path(args.screen)
    out_path = Path(args.out) if args.out else Path.cwd() / "out" / (screen_path.stem + ".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    var_overrides = parse_set_args(args.set)
    root_size_override = None
    if args.root_size:
        m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", args.root_size)
        if not m:
            raise SystemExit(f"--root-size expects WxH (e.g. 1698x798), got: {args.root_size!r}")
        root_size_override = (float(m.group(1)), float(m.group(2)))

    _doc_html, report = render_screen(screen_path, args.sample_data, out_path, args.catalog, var_overrides,
                                       root_size_override=root_size_override, view=args.view,
                                       theme_override=args.theme, theme_file=args.theme_file)
    if args.png:
        to_png(out_path, Path(args.png))
    c = report.counts()
    print(f"Rendered {screen_path} -> {out_path}")
    print(f"  controls: {c['controls']}  defaulted: {c['defaulted']}  unresolved(fallback): {c['unresolved']}  "
          f"unsupported: {c['unknown_controls']}  text-unresolved: {c['text_unresolved']}")


if __name__ == "__main__":
    sys.exit(main() or 0)
