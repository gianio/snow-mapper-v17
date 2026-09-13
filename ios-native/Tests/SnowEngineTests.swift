//  SnowEngineTests.swift
//  Snowmapper — proves the Swift bridge and the JS engine agree.
//
//  This is the test that makes the "one engine, two platforms" claim real
//  rather than aspirational. tools/test_engine.js asserts these exact numbers
//  in node; if Swift disagrees with them, the BRIDGE is wrong (argument order,
//  type conversion, a dropped optional) — not the model. That distinction is
//  what makes failures here easy to diagnose.

import XCTest

final class SnowEngineTests: XCTestCase {

    /// Reference report: powder, N-facing, 1700–2300 m, 2 h old, at Davos.
    private let zone = EngineZone(
        type: "powder", lat: 46.80, lng: 9.83,
        e0: 1700, e1: 2300, asp: 0, conc: 0.85, ageH: 2
    )
    private let lat = 46.80, lon = 9.83

    private func makeEngine() throws -> SnowEngine {
        // Loaded from the TEST bundle, so this runs without launching the app.
        try SnowEngine(bundle: Bundle(for: type(of: self)))
    }

    func testEngineLoadsFromBundle() throws {
        XCTAssertNoThrow(try makeEngine(),
                         "engine.js missing from the test bundle — run ./bootstrap.sh")
    }

    /// The numbers tools/test_engine.js pins: like ≈ 0.982, conf == 96.
    func testMatchesJavaScriptReferenceValues() throws {
        let engine = try makeEngine()
        let score = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                               lat: lat, lon: lon, supporting: [zone]))
        XCTAssertEqual(score.likelihood, 0.982, accuracy: 0.005,
                       "Swift disagrees with the node reference — bridge bug, not model")
        XCTAssertEqual(score.confidence, 96)
        XCTAssertEqual(score.reportCount, 1, accuracy: 0.001)
    }

    func testOppositeAspectIsExcluded() throws {
        let engine = try makeEngine()
        let north = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                               lat: lat, lon: lon, supporting: [zone]))
        let south = try XCTUnwrap(engine.score(aspect: 180, elevation: 2000, slope: 30,
                                               lat: lat, lon: lon, supporting: [zone]))
        XCTAssertLessThan(south.likelihood, 0.05)
        XCTAssertLessThan(south.likelihood, north.likelihood)
    }

    func testAdjacentSectorScoresBetween() throws {
        let engine = try makeEngine()
        let north = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                               lat: lat, lon: lon, supporting: [zone]))
        let northEast = try XCTUnwrap(engine.score(aspect: 45, elevation: 2000, slope: 30,
                                                   lat: lat, lon: lon, supporting: [zone]))
        XCTAssertLessThan(northEast.likelihood, north.likelihood)
        XCTAssertGreaterThan(northEast.likelihood, 0.1)
    }

    func testDistanceAndElevationDecay() throws {
        let engine = try makeEngine()
        let here = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                              lat: lat, lon: lon, supporting: [zone]))
        let farAway = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                                 lat: lat + 40 / 111.0, lon: lon,
                                                 supporting: [zone]))
        let tooHigh = try XCTUnwrap(engine.score(aspect: 0, elevation: 2900, slope: 30,
                                                 lat: lat, lon: lon, supporting: [zone]))
        XCTAssertLessThan(farAway.likelihood, here.likelihood)
        XCTAssertLessThan(tooHigh.likelihood, here.likelihood)
    }

    func testConflictingReportLowersConfidence() throws {
        let engine = try makeEngine()
        var wet = zone
        wet.type = "wet"
        let agreed = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                                lat: lat, lon: lon, supporting: [zone]))
        let disputed = try XCTUnwrap(engine.score(aspect: 0, elevation: 2000, slope: 30,
                                                  lat: lat, lon: lon,
                                                  supporting: [zone], conflicting: [wet]))
        XCTAssertLessThan(disputed.confidence, agreed.confidence)
    }

    func testSetReportsRoundTrips() throws {
        let engine = try makeEngine()
        XCTAssertEqual(engine.setReports([["user_id": "abc"]]), 1)
        XCTAssertEqual(engine.setReports([]), 0)
    }

    func testAspectMatchIsBoundedAndPeaksOnExactMatch() throws {
        let engine = try makeEngine()
        let exact = engine.aspectMatch(0, reported: 0, concentration: 0.85)
        let off = engine.aspectMatch(90, reported: 0, concentration: 0.85)
        XCTAssertEqual(exact, 1.0, accuracy: 0.001, "a perfect aspect match should score 1.0")
        XCTAssertLessThan(off, exact)
        XCTAssertGreaterThanOrEqual(off, 0)
    }

    /// Guards the threading contract: JSContext is not thread-safe, and
    /// SnowEngine funnels every call through one serial queue. If that
    /// serialisation is ever removed this should crash or corrupt.
    func testConcurrentScoringIsSafe() throws {
        let engine = try makeEngine()
        let iterations = 50
        let done = expectation(description: "concurrent scoring")
        done.expectedFulfillmentCount = iterations
        DispatchQueue.concurrentPerform(iterations: iterations) { i in
            let s = engine.score(aspect: Double(i % 360), elevation: 2000, slope: 30,
                                 lat: self.lat, lon: self.lon, supporting: [self.zone])
            XCTAssertNotNil(s)
            done.fulfill()
        }
        wait(for: [done], timeout: 30)
    }
}
