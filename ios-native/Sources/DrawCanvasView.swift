//  DrawCanvasView.swift
//  Snowmapper — PencilKit snow-map drawing over the map.
//
//  ⚠️  NOT YET COMPILED (written on Linux). Architecture is considered; the
//      Swift spelling needs a first pass on a Mac.
//
//  WHY THIS IS THE BIGGEST NATIVE WIN
//  Drawing snow zones on the map is the app's core input mechanism, and the web
//  version is fighting the platform for it: the JS draw tool already reaches for
//  pressure (`drawBrushSize` scales with force) but a WebView only gives a crude
//  approximation, and we spent a whole round fixing two-finger pan lag with
//  requestAnimationFrame throttling — that was the WebView ceiling, not a bug.
//
//  PencilKit gives, for free, what that code approximates by hand:
//    • true pressure, tilt and azimuth per point
//    • palm rejection (draw with the Pencil while resting your hand)
//    • predictive stroke smoothing, so the ink keeps up with the tip
//    • the system ink picker
//  and the pan/zoom gesture conflict disappears because the map view and the
//  canvas are separate UIKit views with their own recognizers.

import PencilKit
import SwiftUI

/// One captured sample: where it was, and how hard the tip was pressed.
/// This is what feeds the zone computation (the equivalent of the web app's
/// `drawRecordSample` → `drawComputeZones`).
public struct DrawSample {
    public let lat: Double
    public let lon: Double
    /// 0…1, normalised from PKStrokePoint.force.
    public let pressure: Double
    /// Which pen was active — maps to the app's snow types
    /// (powder, drift, wet, suncrust, windpressed, firn, scoured).
    public let penType: String
}

/// Converts canvas points to map coordinates. The map view owns the projection,
/// so it supplies this rather than the canvas guessing.
public protocol CanvasProjection {
    func coordinate(at point: CGPoint) -> (lat: Double, lon: Double)?
}

public struct DrawCanvasView: UIViewRepresentable {
    /// The pen the user picked in our own UI; PencilKit's picker handles width
    /// and colour, but the snow TYPE is ours.
    public var penType: String
    public var projection: CanvasProjection
    /// Called as strokes complete, not per point — the zone maths wants whole
    /// strokes and per-point callbacks would be a needless main-thread tax.
    public var onStrokesChanged: ([DrawSample]) -> Void

    public init(penType: String, projection: CanvasProjection,
                onStrokesChanged: @escaping ([DrawSample]) -> Void) {
        self.penType = penType
        self.projection = projection
        self.onStrokesChanged = onStrokesChanged
    }

    public func makeUIView(context: Context) -> PKCanvasView {
        let canvas = PKCanvasView()
        canvas.delegate = context.coordinator
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        // Finger input stays OFF: one finger should pan the map underneath, the
        // Pencil draws. This is what the web app emulates with pointer-type
        // checks and a gesture-pan flag, and it is a single property here.
        canvas.drawingPolicy = .pencilOnly
        canvas.tool = PKInkingTool(.pen, color: .systemBlue, width: 18)
        return canvas
    }

    public func updateUIView(_ canvas: PKCanvasView, context: Context) {
        context.coordinator.penType = penType
        context.coordinator.projection = projection
        context.coordinator.onStrokesChanged = onStrokesChanged
    }

    public func makeCoordinator() -> Coordinator {
        Coordinator(penType: penType, projection: projection, onStrokesChanged: onStrokesChanged)
    }

    public final class Coordinator: NSObject, PKCanvasViewDelegate {
        var penType: String
        var projection: CanvasProjection
        var onStrokesChanged: ([DrawSample]) -> Void

        init(penType: String, projection: CanvasProjection,
             onStrokesChanged: @escaping ([DrawSample]) -> Void) {
            self.penType = penType
            self.projection = projection
            self.onStrokesChanged = onStrokesChanged
        }

        public func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            let samples = Self.samples(from: canvasView.drawing,
                                       penType: penType,
                                       projection: projection)
            onStrokesChanged(samples)
        }

        /// Walks the strokes and thins them to roughly one sample per few points.
        /// The zone maths groups samples by pressure-derived depth, so it wants
        /// coverage, not every interpolated point PencilKit generates.
        static func samples(from drawing: PKDrawing, penType: String,
                            projection: CanvasProjection, stride: Int = 4) -> [DrawSample] {
            var out: [DrawSample] = []
            for stroke in drawing.strokes {
                let path = stroke.path
                var i = 0
                while i < path.count {
                    let p = path[i]
                    // PencilKit gives points in the stroke's own space; the
                    // stroke transform puts them back into canvas space.
                    let canvasPoint = p.location.applying(stroke.transform)
                    if let c = projection.coordinate(at: canvasPoint) {
                        // force is 0 for a finger and can exceed 1 with a firm
                        // Pencil press, so clamp rather than trust it.
                        let pressure = max(0, min(1, Double(p.force)))
                        out.append(DrawSample(lat: c.lat, lon: c.lon,
                                              pressure: pressure, penType: penType))
                    }
                    i += stride
                }
            }
            return out
        }
    }
}
