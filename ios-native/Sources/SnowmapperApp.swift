//  SnowmapperApp.swift
//  Snowmapper — native app entry point.
//
//  WHAT THIS SCREEN IS
//  Not the product — a deliberate DEV HARNESS, and the right thing to build
//  first. It exercises exactly the three things that cannot be verified without
//  running on real hardware:
//
//    1. the JavaScriptCore bridge actually loads engine.js on a device and
//       returns the same numbers CI asserts (0.98 likelihood / 96 confidence)
//    2. every haptic, so they can be FELT and retuned — the values in
//       HapticEngine are considered guesses until someone holds the phone
//    3. the PencilKit canvas with a real Apple Pencil, which no simulator can
//       reproduce (no pressure, no tilt)
//
//  The real UI comes after the MapKit-vs-Mapbox decision. This exists so the
//  first thing you run tells you whether the foundations work.

import SwiftUI

@main
struct SnowmapperApp: App {
    var body: some Scene {
        WindowGroup {
            HarnessView()
        }
    }
}

// MARK: - Harness

struct HarnessView: View {
    @State private var engine: SnowEngine?
    @State private var engineError: String?

    // Reference scenario, identical to tools/test_engine.js and to the example
    // in ios-native/README.md, so the expected answer is already known.
    @State private var aspect: Double = 0
    @State private var elevation: Double = 2000
    @State private var slope: Double = 30

    @State private var showDraw = false
    @State private var drawSampleCount = 0

    private let referenceZone = EngineZone(
        type: "powder", lat: 46.80, lng: 9.83,
        e0: 1700, e1: 2300, asp: 0, conc: 0.85, ageH: 2
    )

    private var score: PowderScore? {
        engine?.score(aspect: aspect, elevation: elevation, slope: slope,
                      lat: 46.80, lon: 9.83, supporting: [referenceZone])
    }

    var body: some View {
        NavigationStack {
            Form {
                engineSection
                hapticsSection
                drawSection
            }
            .navigationTitle("Snowmapper — harness")
            .sheet(isPresented: $showDraw) { drawSheet }
        }
        .onAppear(perform: loadEngine)
    }

    // MARK: Engine

    private var engineSection: some View {
        Section("Engine (JavaScriptCore)") {
            if let engineError {
                Label(engineError, systemImage: "xmark.octagon")
                    .foregroundStyle(.red)
                Text("engine.js missing from the bundle? Run ./bootstrap.sh, which "
                     + "generates it via tools/make_engine.py.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if let score {
                LabeledContent("Likelihood", value: String(format: "%.3f", score.likelihood))
                LabeledContent("Confidence", value: "\(score.confidence)%")
                LabeledContent("Reports", value: String(format: "%.2f", score.reportCount))
                if let cm = score.centimetres {
                    LabeledContent("Depth", value: String(format: "%.0f cm", cm))
                }
                // The one assertion that matters on first run: Swift and JS agree.
                let matchesCI = abs(score.likelihood - 0.982) < 0.01 && score.confidence == 96
                if aspect == 0 && elevation == 2000 && slope == 30 {
                    Label(matchesCI ? "Matches CI (0.982 / 96%)" : "DIFFERS from CI — bridge bug",
                          systemImage: matchesCI ? "checkmark.seal" : "exclamationmark.triangle")
                        .foregroundStyle(matchesCI ? .green : .orange)
                        .font(.caption)
                }
            } else {
                ProgressView()
            }

            VStack(alignment: .leading) {
                Text("Aspect \(Int(aspect))°").font(.caption)
                Slider(value: $aspect, in: 0...359, step: 1)
                Text("Elevation \(Int(elevation)) m").font(.caption)
                Slider(value: $elevation, in: 500...4000, step: 50)
                Text("Slope \(Int(slope))°").font(.caption)
                Slider(value: $slope, in: 0...55, step: 1)
            }
            Text("Reported: powder, N-facing, 1700–2300 m, 2 h old.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func loadEngine() {
        guard engine == nil else { return }
        do {
            engine = try SnowEngine()
        } catch {
            engineError = "Engine failed to load: \(error)"
        }
    }

    // MARK: Haptics

    private var hapticsSection: some View {
        Section("Haptics (feel these, then retune HapticEngine)") {
            hapticRow("Selection tick", "Timeline scrubbing, per day boundary") {
                HapticEngine.shared.selectionTick()
            }
            hapticRow("Success", "Report posted") { HapticEngine.shared.success() }
            hapticRow("Failure", "Post rejected") { HapticEngine.shared.failure() }
            hapticRow("Warning", "Steep terrain nearby") { HapticEngine.shared.warning() }
            hapticRow("Pen down", "Stroke registered") { HapticEngine.shared.penDown() }
            hapticRow("Zone complete", "Drawn zone accepted") { HapticEngine.shared.zoneComplete() }
            hapticRow("Snow texture — deep powder", "80 cm, soft") {
                HapticEngine.shared.snowTexture(depthCm: 80, crusty: 0.05)
            }
            hapticRow("Snow texture — thin crust", "15 cm, hard") {
                HapticEngine.shared.snowTexture(depthCm: 15, crusty: 0.95)
            }
        }
    }

    private func hapticRow(_ title: String, _ subtitle: String,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Draw

    private var drawSection: some View {
        Section("Draw (PencilKit — needs a real Apple Pencil)") {
            Button("Open canvas") {
                HapticEngine.shared.prepare()
                showDraw = true
            }
            LabeledContent("Captured samples", value: "\(drawSampleCount)")
            Text("A finger pans; only the Pencil draws (drawingPolicy = .pencilOnly). "
                 + "Simulators produce no pressure, so this needs an iPad.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var drawSheet: some View {
        NavigationStack {
            DrawCanvasView(penType: "powder",
                           projection: PlaceholderProjection()) { samples in
                drawSampleCount = samples.count
            }
            .ignoresSafeArea(edges: .bottom)
            .navigationTitle("Draw a zone")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        HapticEngine.shared.zoneComplete()
                        showDraw = false
                    }
                }
            }
        }
    }
}

/// Stands in for the map's projection until there IS a map. Maps canvas points
/// linearly onto a small box around Davos purely so the capture path can be
/// exercised end to end; it is not a real projection.
struct PlaceholderProjection: CanvasProjection {
    func coordinate(at point: CGPoint) -> (lat: Double, lon: Double)? {
        let lat = 46.85 - (Double(point.y) / 800.0) * 0.10
        let lon = 9.78 + (Double(point.x) / 400.0) * 0.10
        return (lat: lat, lon: lon)
    }
}
