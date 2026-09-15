"""Carve the scoring engine out of the app script — with NO dependencies.

This module is deliberately import-light (stdlib only). `interactive_export`
needs numpy, rasterio, pyproj, scipy and GDAL, which is a heavy and
occasionally painful install on macOS — and needing all of that just to emit a
10 KB JavaScript file would make the native iOS setup far more annoying than it
has to be. So the engine wrapper and its markers live here, and two callers
share them:

  * `pipeline.interactive_export._engine_js()` — during a full pipeline build
  * `tools/make_engine.py` — reads interactive_export.py as TEXT and needs
    nothing installed at all

Both produce byte-identical output; `tools/test_engine_extract.py` asserts it,
so the two paths cannot drift apart.
"""
from __future__ import annotations

# Sentinels around the whole application script inside the HTML template.
APP_START = "/*__APP_START__*/"
APP_END = "/*__APP_END__*/"

# The terrain-similarity scoring block. tools/test_model.js has always relied on
# these same markers to run the model outside a browser.
ENGINE_START = "// --- Report credibility"
ENGINE_END = "function renderPrognosis"

# Pure scoring only. progZones() is deliberately NOT exported: it reads app
# state (progSrc, demoFitZones, progZoneFromPoint, progAgeH) and converts app
# report objects into zone records, which is glue, not model. A consumer builds
# the zone list itself and hands it to progCell.
ENGINE_EXPORTS = (
    "progCell", "progEnvelope", "progAspectMatch", "progElevMatch",
    "progSlopeMatch", "progRecency", "progDistKm", "progReportWeight",
    "progTrustOf", "progTrustMap", "progInvalidateTrust",
    # Tour scoring: the pure half only. tourSampleSeg/tourScoreRoute read app
    # state (the grids, computePowder, the bulletin) and are glue, not model --
    # a consumer samples the segments itself and hands them to tourAggregate.
    "tourResample", "tourAggregate", "tourVerdict", "tourDistM", "tourIsDescent",
)


# Where the HTML template is assigned. The search has to start here rather than
# at the top of the file: _app_js() contains the APP_START/APP_END sentinels as
# string LITERALS in its own body, and that function is defined *above* the
# template — so a naive split on the first occurrence returns a fragment of
# Python source instead of the app script.
HTML_ASSIGN = '_HTML = r"""'


def app_js_from_source(source_text: str) -> str:
    """Slice the app script out of interactive_export.py's raw source text.

    Used by the no-dependency path, which cannot import the module to reach
    `_HTML` directly.
    """
    start = source_text.find(HTML_ASSIGN)
    if start < 0:
        raise RuntimeError(
            "could not find the HTML template assignment (%r). If it was renamed, "
            "update HTML_ASSIGN." % HTML_ASSIGN)
    try:
        after = source_text[start:].split(APP_START, 1)[1]
        return after.split(APP_END, 1)[0]
    except IndexError as exc:
        raise RuntimeError(
            "could not find the app script between %r and %r inside the HTML template"
            % (APP_START, APP_END)) from exc


def build_engine_js(app_js: str) -> str:
    """Wrap the scoring block as a UMD-ish module.

    Raises if the markers move or an expected export is missing, so a rename
    breaks the build loudly instead of silently shipping an empty engine.
    """
    i = app_js.find(ENGINE_START)
    j = app_js.find(ENGINE_END, i) if i >= 0 else -1
    if i < 0 or j < 0:
        raise RuntimeError(
            "engine.js: could not locate the model block between %r and %r in the app "
            "script. If those markers were renamed, update ENGINE_START/ENGINE_END."
            % (ENGINE_START, ENGINE_END))
    block = app_js[i:j]
    missing = [name for name in ENGINE_EXPORTS if ("function " + name) not in block]
    if missing:
        raise RuntimeError("engine.js: expected functions missing from the model "
                           "block: %s" % ", ".join(missing))
    return (
        "// Snowmapper scoring engine — GENERATED from the app script, do not edit.\n"
        "// Loads in node, in a worker, and in JavaScriptCore (no DOM, no Leaflet).\n"
        "// Feed it reports with setReports(), then score a point with progCell().\n"
        "(function(root,factory){\n"
        "  if(typeof module==='object'&&module.exports){module.exports=factory();}\n"
        "  else{root.SnowEngine=factory();}\n"
        "})(typeof globalThis!=='undefined'?globalThis:this,function(){\n"
        "  'use strict';\n"
        "  // Grid dimensions are the only app-level values the block reads; they\n"
        "  // matter for progZones (not exported) and are harmless defaults here.\n"
        "  var PROG_GW=340,PROG_GH=240;\n"
        "  // progTrustMap/progReportWeight score authors from the report set, so\n"
        "  // the consumer supplies it rather than the module reaching for a global.\n"
        "  var allReports=[];\n"
        + block +
        "\n  return {\n"
        "    " + ", ".join(ENGINE_EXPORTS) + ",\n"
        "    setReports:function(rs){allReports=Array.isArray(rs)?rs:[];"
        "try{progInvalidateTrust();}catch(e){}return allReports.length;},\n"
        "    getReports:function(){return allReports;}\n"
        "  };\n"
        "});\n"
    )
