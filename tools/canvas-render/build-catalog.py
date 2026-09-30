#!/usr/bin/env python3
"""
build-catalog.py -- (re)generate defaults-catalog.json for the canvas-design toolkit.

This is the CALIBRATION tool. It is NOT run automatically by render-screen.py or
lint-canvas.py -- they only ever read the committed defaults-catalog.json. Run this
script by hand when:
  - a new control template@version shows up in an app (unknown-control
    warnings from render-screen.py), or
  - a user reports a rendered box that doesn't match what Studio actually shows
    (see calibrate.py).

Seed sources, in priority order (recorded per-entry as "source" so calibration can
tell what to trust):
  1. "theme"     -- literal defaultValue="..." attributes pulled out of the packed
                    .msapp files' References/Templates.json (UsedTemplates[].Template,
                    an XML control-template document). This is Studio's own compiled
                    default for that control@version -- highest confidence.
  2. "inferred"  -- for properties the template XML leaves blank (common for the
                    "Modern*" Fluent controls, which are themed at runtime rather than
                    templated), we scan every .pa.yaml source under --src and take the
                    statistical mode of whatever literal (non-formula) value authors
                    actually used for that control@version + property. Not authoritative,
                    but a reasonable guess grounded in this codebase's own conventions.
  3. "documented"-- a small number of hand-entered values for properties neither of the
                    above could produce, taken from Microsoft's published control
                    defaults documentation.
  4. (none)      -- if nothing above produces a value, the property is simply absent
                    from the catalog. render-screen.py then uses its own hardcoded
                    "conservative fallback" and VISIBLY flags the control (dashed amber
                    outline + report entry) -- this is the case the catalog is meant to
                    shrink over time.

Usage:
    python build-catalog.py [--repo-root ...] [--out defaults-catalog.json]
"""
import argparse
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    raise

# Properties we care about for LAYOUT/TYPOGRAPHY preview. Anything else pulled out of
# a template/corpus scan is discarded -- the catalog stays focused on what
# render-screen.py actually consults.
TRACKED_PROPS = {
    "Width", "Height", "Size", "Color", "Fill", "BorderColor", "BorderThickness",
    "RadiusTopLeft", "RadiusTopRight", "RadiusBottomLeft", "RadiusBottomRight",
    "FontWeight", "Align", "Wrap", "PaddingTop", "PaddingRight", "PaddingBottom",
    "PaddingLeft", "TemplateSize", "TemplatePadding", "VerticalAlign",
}

# A handful of values genuinely aren't in any packed template and aren't common enough
# in a typical corpus to infer reliably. These are Microsoft's documented defaults for the
# named control@version (source="documented").
DOCUMENTED = {
    # Label's packed template leaves Align/FontWeight blank (no defaultValue attr at
    # all) -- corpus-mode "inferred" is badly skewed here because most Labels in
    # apps that bother to set Align/FontWeight explicitly are section headers (Right /
    # Semibold), while the much larger population of plain body labels never sets
    # either and so never enters the corpus sample. Studio's actual unset defaults are
    # Left / Normal -- override the corpus guess.
    "Label@2.5.1": {
        "Align": "Left", "FontWeight": "Normal",
        "RadiusTopLeft": 0, "RadiusTopRight": 0, "RadiusBottomLeft": 0, "RadiusBottomRight": 0,
    },
    # Same shape of bias: most GroupContainers that set BorderThickness explicitly are
    # the ones drawing a visible card border; borderless layout containers (the
    # majority) never set it and so never get counted. Studio's real unset default is
    # an invisible (0px) border.
    "GroupContainer@1.5.0": {"BorderThickness": 0},
    "ModernButton@1.0.0": {"Color": "#ffffff", "Fill": "#242424", "FontWeight": "Normal", "Align": "Center",
                           "BorderColor": "#8a8886"},
    "ModernTextInput@1.1.1": {"Color": "#242424", "Fill": "#ffffff", "BorderColor": "#8a8886", "BorderThickness": 1},
    "ModernDropdown@1.0.2": {"Color": "#242424", "Fill": "#ffffff", "BorderColor": "#8a8886", "BorderThickness": 1},
    "ModernDatePicker@1.0.0": {"Color": "#242424", "Fill": "#ffffff", "BorderColor": "#8a8886", "BorderThickness": 1,
                               "RadiusTopLeft": 4, "RadiusTopRight": 4, "RadiusBottomLeft": 4, "RadiusBottomRight": 4,
                               "Size": 14},
    "ModernDatePicker@1.0.1": {"Color": "#242424", "Fill": "#ffffff", "BorderColor": "#8a8886", "BorderThickness": 1,
                               "RadiusTopLeft": 4, "RadiusTopRight": 4, "RadiusBottomLeft": 4, "RadiusBottomRight": 4},
    "GroupContainer@1.5.0": {"Fill": "rgba(0,0,0,0)", "BorderColor": "#000000"},
    "Rectangle@2.3.0": {"Color": "#ffffff", "BorderThickness": 0,
                        "RadiusTopLeft": 0, "RadiusTopRight": 0, "RadiusBottomLeft": 0, "RadiusBottomRight": 0},
    "Gallery@2.15.0": {"TemplatePadding": 0, "TemplateSize": 100, "Fill": "rgba(0,0,0,0)"},
    "HtmlViewer@2.1.0": {"BorderColor": "#000000",
                         "RadiusTopLeft": 0, "RadiusTopRight": 0, "RadiusBottomLeft": 0, "RadiusBottomRight": 0},
    "Image@2.3.0": {"Width": 200, "Height": 200},
    "Circle@2.3.0": {"Fill": "#00b0f0", "Width": 100, "Height": 100},
    "Attachments@2.3.0": {},
    "TypedDataCard@1.0.7": {"Width": 220, "Height": 60},
    "RichTextEditor@2.7.0": {"Color": "#000000"},
    "Timer@2.1.0": {},
    "Form@2.4.4": {},
}

INCLUDE_PROP_RE = re.compile(r"<[\w:]*includeProperty\b([^>]*?)/?>")
PROP_RE = re.compile(r"<[\w:]*property\b([^>]*?)(?:/>|>)")
NAME_RE = re.compile(r'name="([^"]+)"')
DEFAULT_RE = re.compile(r'defaultValue="([^"]*)"')

NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")

# The packed template's internal "Name" doesn't always match the Control tag authors
# see in .pa.yaml -- Classic controls in particular are prefixed "Classic/" in source
# but stored under their bare AppMagic name in the template.
TEMPLATE_NAME_OVERRIDE = {
    "button": "Classic/Button",
    "text": "Classic/TextInput",
}


def coerce_scalar(raw):
    """Best-effort: turn a raw defaultValue/inferred string into a JSON-friendly scalar."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    if NUMERIC_RE.match(raw):
        return float(raw) if "." in raw else int(raw)
    if raw in ("true", "false"):
        return raw == "true"
    # RGBA(r, g, b, a) literal straight out of a template -> css rgba()
    m = re.match(r"RGBA\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)", raw)
    if m:
        r, g, b, a = m.groups()
        return f"rgba({int(float(r))},{int(float(g))},{int(float(b))},{a})"
    # Enum reservations like %FontWeight.RESERVED%.Normal / %Align.RESERVED%.Center
    m = re.match(r"%(\w+)\.RESERVED%\.(\w+)", raw)
    if m:
        return m.group(2)
    # Self.X / ColorFade(...) / other formula defaults aren't representable as a
    # static scalar -- treat as "no usable literal default".
    if raw.startswith("Self.") or "(" in raw or raw.startswith("#") is False and raw.startswith("%"):
        return None
    return raw


def extract_theme_seed(msapp_paths):
    """Pull includeProperty/property defaultValue="..." out of every packed control
    template in every .msapp, merged by Name@Version. First .msapp to define a given
    Name@Version wins (apps package the same Studio-generated templates, so in
    practice there's no real conflict)."""
    seed = {}
    for p in msapp_paths:
        try:
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist() if n.replace("\\", "/").endswith("References/Templates.json")]
                for n in names:
                    raw = z.read(n)
                    text = raw.decode("utf-8-sig")
                    data = json.loads(text)
                    for t in data.get("UsedTemplates", []):
                        name = t.get("Name")
                        version = t.get("Version")
                        xml = t.get("Template", "")
                        if not name or not version:
                            continue
                        display_name = TEMPLATE_NAME_OVERRIDE.get(name, name[0].upper() + name[1:])
                        key = f"{display_name}@{version}"
                        props = seed.setdefault(key, {})
                        for rx in (INCLUDE_PROP_RE, PROP_RE):
                            for m in rx.finditer(xml):
                                attrs = m.group(1)
                                nm = NAME_RE.search(attrs)
                                dv = DEFAULT_RE.search(attrs)
                                if not nm or not dv:
                                    continue
                                pname = nm.group(1)
                                if pname not in TRACKED_PROPS:
                                    continue
                                if pname in props:
                                    continue  # first template wins
                                val = coerce_scalar(dv.group(1))
                                if val is not None:
                                    props[pname] = val
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
            continue
    return seed


def literal_value(raw):
    """If a pa.yaml property's formula text (after stripping '=') is a bare literal
    (number, quoted string, True/False, or a simple Enum.Member path), return a
    JSON-friendly scalar for corpus statistics. Otherwise None (it's a real formula --
    skip it, we only want to infer from what authors typed as plain constants)."""
    raw = raw.strip()
    if raw.startswith("="):
        raw = raw[1:]
    raw = raw.strip()
    if raw == "":
        return None
    if NUMERIC_RE.match(raw):
        return float(raw) if "." in raw else int(raw)
    if raw in ("True", "False"):
        return raw == "True"
    m = re.match(r'^"([^"]*)"$', raw)
    if m:
        return m.group(1)
    # varTheme.X / RGBA(...) / ColorValue(...) are real color formulas, not literals we
    # can average -- skip for corpus stats (colors get "documented" fallbacks instead).
    m = re.match(r"^(FontWeight|Align|VerticalAlign)\.(\w+)$", raw)
    if m:
        return m.group(2)
    return None


def scan_corpus(pa_yaml_paths):
    """For each Control@Version + property, collect a Counter of literal values seen
    across every screen source in the repo. Returns {key: {prop: Counter}}."""
    stats = defaultdict(lambda: defaultdict(Counter))

    def walk(node):
        if isinstance(node, dict):
            if "Control" in node and "Properties" in node:
                ctrl = node["Control"]
                key = ctrl if "@" in ctrl else ctrl
                # Normalize "Module.Action" style names aren't used here; Control is
                # already "Name@Version" or "Name.Selector@Version" for classic ctrls.
                props = node.get("Properties") or {}
                for pname, pval in props.items():
                    if pname not in TRACKED_PROPS or not isinstance(pval, str):
                        continue
                    lit = literal_value(pval)
                    if lit is not None:
                        stats[key][pname][lit] += 1
                for child in node.get("Children") or []:
                    walk(child)
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for p in pa_yaml_paths:
        try:
            text = p.read_text(encoding="utf-8-sig")
            doc = yaml.safe_load(text)
        except Exception:
            continue
        walk(doc)

    inferred = {}
    for key, propmap in stats.items():
        entry = {}
        for pname, counter in propmap.items():
            if not counter:
                continue
            mode_val, _ = counter.most_common(1)[0]
            entry[pname] = mode_val
        if entry:
            inferred[key] = entry
    return inferred


def build(repo_root: Path):
    msapp_paths = sorted(repo_root.glob("**/*.msapp"))
    pa_yaml_paths = sorted(p for p in repo_root.glob("**/*.pa.yaml") if "_EditorState" not in p.name)

    theme_seed = extract_theme_seed(msapp_paths)
    inferred_seed = scan_corpus(pa_yaml_paths)

    all_keys = set(theme_seed) | set(inferred_seed) | set(DOCUMENTED)
    catalog = {}
    stats = Counter()
    for key in sorted(all_keys):
        entry = {}
        theme_props = theme_seed.get(key, {})
        inferred_props = inferred_seed.get(key, {})
        documented_props = DOCUMENTED.get(key, {})
        for pname in TRACKED_PROPS:
            if pname in theme_props:
                entry[pname] = {"value": theme_props[pname], "source": "theme"}
                stats["theme"] += 1
            elif pname in documented_props:
                entry[pname] = {"value": documented_props[pname], "source": "documented"}
                stats["documented"] += 1
            elif pname in inferred_props:
                entry[pname] = {"value": inferred_props[pname], "source": "inferred"}
                stats["inferred"] += 1
        if entry:
            catalog[key] = entry

    return catalog, stats, len(msapp_paths), len(pa_yaml_paths)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", "--repo-root", dest="repo_root", required=True,
                    help="folder holding the apps to learn from (*.msapp and **/*.pa.yaml)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "defaults-catalog.json"))
    args = ap.parse_args()

    repo_root = Path(args.repo_root)
    catalog, stats, n_msapp, n_yaml = build(repo_root)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"Scanned {n_msapp} .msapp files and {n_yaml} .pa.yaml sources.")
    print(f"Catalog entries: {len(catalog)} control@version keys")
    print(f"Property-value seeds by source: {dict(stats)}")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
