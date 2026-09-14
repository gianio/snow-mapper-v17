//  SnowEngine.swift
//  Snowmapper — native bridge to the shared scoring engine.
//
//  ⚠️  NOT YET COMPILED. Written on Linux, where Apple frameworks do not exist.
//      Expect small fixes on the first `xcodebuild`. The ARCHITECTURE and the
//      JS API surface are verified (see tools/test_engine.js); the Swift
//      spelling is not.
//
//  WHY JAVASCRIPTCORE AND NOT A SWIFT PORT
//  The terrain-similarity model is the app's crown jewels and it changes often —
//  we have been tuning aspect falloff, credibility weighting and recency for
//  months. Reimplementing it in Swift would mean two implementations drifting
//  apart, and every future tweak done twice. Instead the pipeline emits
//  `engine.js` (a DOM-free module, ~10 KB) and this class runs it in
//  JavaScriptCore — the same engine that powers Safari, no WebView, no UI, and
//  entirely App Store legal. One source of truth for the physics.
//
//  The JS surface this wraps is asserted by tools/test_engine.js in CI, so if
//  someone changes the engine's shape, CI fails before this does.

import Foundation
import JavaScriptCore

/// One scored point: `progCell()`'s return value, `{like, conf, cm, n}`.
public struct PowderScore: Equatable {
    /// 0…1 — how likely the reported condition is at this point.
    public let likelihood: Double
    /// 0…100 — how much evidence backs that.
    public let confidence: Int
    /// Depth in cm, averaged over contributing reports. `nil` when none carried one.
    public let centimetres: Double?
    /// Effective number of contributing reports.
    public let reportCount: Double
}

/// A zone as the engine expects it — the shape `progZones()` produces in the
/// web app. Building this list is app glue, deliberately NOT part of the engine
/// (it reads app state), so the native app owns it.
public struct EngineZone {
    public var type: String
    public var lat: Double
    public var lng: Double
    /// Elevation band, metres.
    public var e0: Double?
    public var e1: Double?
    /// Aspect in degrees (0 = N) and how tightly the report is concentrated (0…1).
    public var asp: Double?
    public var conc: Double?
    /// Slope angle and its spread, degrees.
    public var slp: Double?
    public var slpSd: Double?
    /// Reported depth, cm.
    public var cm: Double?
    /// Age in hours — drives recency decay.
    public var ageH: Double?
    /// Report weight (credibility × confirmations); 1 = neutral.
    public var w: Double?

    public init(type: String, lat: Double, lng: Double, e0: Double? = nil, e1: Double? = nil,
                asp: Double? = nil, conc: Double? = nil, slp: Double? = nil, slpSd: Double? = nil,
                cm: Double? = nil, ageH: Double? = nil, w: Double? = nil) {
        self.type = type; self.lat = lat; self.lng = lng
        self.e0 = e0; self.e1 = e1; self.asp = asp; self.conc = conc
        self.slp = slp; self.slpSd = slpSd; self.cm = cm; self.ageH = ageH; self.w = w
    }

    /// JSON-ish dictionary; `nil` stays absent so the JS defaults apply.
    var jsObject: [String: Any] {
        var d: [String: Any] = ["type": type, "lat": lat, "lng": lng]
        if let e0 { d["e0"] = e0 };       if let e1 { d["e1"] = e1 }
        if let asp { d["asp"] = asp };    if let conc { d["conc"] = conc }
        if let slp { d["slp"] = slp };    if let slpSd { d["slpSd"] = slpSd }
        if let cm { d["cm"] = cm };       if let ageH { d["ageH"] = ageH }
        if let w { d["w"] = w }
        return d
    }
}

public enum SnowEngineError: Error {
    case engineScriptMissing
    case evaluationFailed(String)
    case globalMissing
}

/// Thread-safety: a `JSContext` is not safe to use from several threads at
/// once, so every call funnels through one serial queue. Scoring a full grid
/// should therefore be done in one batch call, not thousands of hops.
public final class SnowEngine {
    private let queue = DispatchQueue(label: "ch.snowmapper.engine")
    private let context: JSContext
    private let engine: JSValue

    /// Loads `engine.js`. Ship it in the app bundle next to the web assets;
    /// it is emitted by the same pipeline build (`dist/engine.js`).
    public init(engineScript: String) throws {
        guard let context = JSContext() else { throw SnowEngineError.evaluationFailed("no JSContext") }
        self.context = context

        var thrown: String?
        context.exceptionHandler = { _, exception in
            thrown = exception?.toString() ?? "unknown JS exception"
        }
        context.evaluateScript(engineScript)
        if let thrown { throw SnowEngineError.evaluationFailed(thrown) }

        // The module's UMD wrapper assigns onto globalThis when there is no
        // CommonJS `module`, which is the case here.
        guard let engine = context.objectForKeyedSubscript("SnowEngine"), !engine.isUndefined else {
            throw SnowEngineError.globalMissing
        }
        self.engine = engine
    }

    /// Convenience: load from the app bundle.
    public convenience init(bundle: Bundle = .main, resource: String = "engine", ext: String = "js") throws {
        guard let url = bundle.url(forResource: resource, withExtension: ext),
              let src = try? String(contentsOf: url, encoding: .utf8) else {
            throw SnowEngineError.engineScriptMissing
        }
        try self.init(engineScript: src)
    }

    /// Feeds the engine the report set its credibility/trust maths reads.
    /// Pass the raw reports (needing `user_id`), not the zone list.
    @discardableResult
    public func setReports(_ reports: [[String: Any]]) -> Int {
        queue.sync {
            let result = engine.invokeMethod("setReports", withArguments: [reports])
            return Int(result?.toInt32() ?? 0)
        }
    }

    /// Scores one point against the supporting and conflicting zones.
    /// Mirrors `progCell(asp, elev, slp, lat, lon, sel, oth)`.
    public func score(aspect: Double, elevation: Double, slope: Double,
                      lat: Double, lon: Double,
                      supporting: [EngineZone], conflicting: [EngineZone] = []) -> PowderScore? {
        queue.sync {
            let args: [Any] = [aspect, elevation, slope, lat, lon,
                               supporting.map(\.jsObject), conflicting.map(\.jsObject)]
            guard let r = engine.invokeMethod("progCell", withArguments: args), r.isObject else { return nil }
            let cm = r.objectForKeyedSubscript("cm")
            return PowderScore(
                likelihood: r.objectForKeyedSubscript("like")?.toDouble() ?? 0,
                confidence: Int(r.objectForKeyedSubscript("conf")?.toInt32() ?? 0),
                centimetres: (cm?.isNull ?? true) ? nil : cm?.toDouble(),
                reportCount: r.objectForKeyedSubscript("n")?.toDouble() ?? 0
            )
        }
    }

    /// How well a slope's aspect matches a reported one — useful on its own for
    /// per-slope readouts without scoring a whole cell.
    public func aspectMatch(_ aspect: Double, reported: Double, concentration: Double = 0.7) -> Double {
        queue.sync {
            engine.invokeMethod("progAspectMatch",
                                withArguments: [aspect, reported, concentration])?.toDouble() ?? 0
        }
    }
}
