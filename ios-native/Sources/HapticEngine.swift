//  HapticEngine.swift
//  Snowmapper — CoreHaptics feedback.
//
//  WHY NATIVE HAPTICS BEAT THE WRAPPER
//  Capacitor's Haptics plugin gives three impact strengths, three notification
//  patterns and a selection tick. That is already a big step up from the web app
//  (iOS Safari ignores navigator.vibrate entirely, so on the web there is no
//  haptic feedback at all). But every one of those is a fixed, canned pattern.
//
//  CoreHaptics lets us play *composed* haptics with continuous intensity and
//  sharpness envelopes. For this app that unlocks one thing the wrapper cannot
//  do at all: feedback whose TEXTURE carries data. Dragging across the map can
//  feel soft and dull over deep settled snow and sharp and brittle over a
//  wind-crust — which is information you can take in with gloves on, without
//  looking at the screen. That is the kind of thing a ski-touring app should do.
//
//  ⚠️  Compile-checked by .github/workflows/swift-build.yml, but NEVER FELT.
//      I cannot test haptics — the intensity/sharpness values below are
//      considered starting points, not tuned ones. Expect to adjust them on a
//      real device; that part is yours.

import CoreHaptics
import UIKit

public final class HapticEngine {
    public static let shared = HapticEngine()

    private var engine: CHHapticEngine?
    private let supportsHaptics: Bool
    /// UIKit generators are the fallback on hardware without CoreHaptics
    /// (older devices, iPad) and they are cheap to keep around.
    private let impactLight = UIImpactFeedbackGenerator(style: .light)
    private let impactMedium = UIImpactFeedbackGenerator(style: .medium)
    private let impactHeavy = UIImpactFeedbackGenerator(style: .heavy)
    private let selection = UISelectionFeedbackGenerator()
    private let notification = UINotificationFeedbackGenerator()

    private init() {
        supportsHaptics = CHHapticEngine.capabilitiesForHardware().supportsHaptics
        guard supportsHaptics else { return }
        do {
            let engine = try CHHapticEngine()
            // The engine is stopped whenever the app backgrounds or the system
            // reclaims it. Without these handlers the first haptic after coming
            // back from the lock screen silently does nothing — a classic and
            // very confusing CoreHaptics bug.
            engine.stoppedHandler = { [weak self] _ in self?.engine = nil }
            // `_ =` matters: a single-expression closure would otherwise infer
            // `Void?` as its return type and not match `(() -> Void)?`.
            engine.resetHandler = { [weak self] in _ = try? self?.engine?.start() }
            try engine.start()
            self.engine = engine
        } catch {
            self.engine = nil
        }
    }

    /// Re-acquires the engine if the system took it away.
    private func ready() -> CHHapticEngine? {
        if let engine { return engine }
        guard supportsHaptics else { return nil }
        do {
            let engine = try CHHapticEngine()
            engine.stoppedHandler = { [weak self] _ in self?.engine = nil }
            engine.resetHandler = { [weak self] in _ = try? self?.engine?.start() }
            try engine.start()
            self.engine = engine
            return engine
        } catch {
            return nil
        }
    }

    private func play(_ events: [CHHapticEvent]) {
        guard let engine = ready() else { return }
        do {
            let pattern = try CHHapticPattern(events: events, parameters: [])
            let player = try engine.makePlayer(with: pattern)
            try player.start(atTime: CHHapticTimeImmediate)
        } catch {
            // Fall back rather than silently doing nothing.
            impactLight.impactOccurred()
        }
    }

    // MARK: - Semantic feedback
    // Named for what happened, not for how it feels, so call sites read clearly
    // and the feel can be retuned in one place.

    /// A discrete tick. Use while dragging the timeline across hour/day
    /// boundaries — this is the single biggest feel upgrade in the app, because
    /// it lets you land on a time without watching the slider.
    public func selectionTick() {
        guard supportsHaptics else { return selection.selectionChanged() }
        play([CHHapticEvent(eventType: .hapticTransient, parameters: [
            CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.45),
            CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.75),
        ], relativeTime: 0)])
    }

    /// A report posted, an endorsement landed — something succeeded.
    /// Two rising taps read as "done" more clearly than one.
    public func success() {
        guard supportsHaptics else { return notification.notificationOccurred(.success) }
        play([
            CHHapticEvent(eventType: .hapticTransient, parameters: [
                CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.5),
                CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.4),
            ], relativeTime: 0),
            CHHapticEvent(eventType: .hapticTransient, parameters: [
                CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.9),
                CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.6),
            ], relativeTime: 0.09),
        ])
    }

    /// A post failed, validation rejected the input.
    public func failure() {
        guard supportsHaptics else { return notification.notificationOccurred(.error) }
        play([
            CHHapticEvent(eventType: .hapticTransient, parameters: [
                CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.9),
                CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.9),
            ], relativeTime: 0),
            CHHapticEvent(eventType: .hapticTransient, parameters: [
                CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.6),
                CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.3),
            ], relativeTime: 0.12),
        ])
    }

    /// Steep terrain, a danger sign nearby. Deliberately restrained: a scary
    /// buzz for every glance at a slope would get ignored within a day.
    public func warning() {
        guard supportsHaptics else { return notification.notificationOccurred(.warning) }
        play([CHHapticEvent(eventType: .hapticContinuous, parameters: [
            CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.55),
            CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.2),
        ], relativeTime: 0, duration: 0.22)])
    }

    /// Pencil touched down on the draw canvas — confirms the stroke registered
    /// while you are looking at terrain rather than at the screen.
    public func penDown() {
        guard supportsHaptics else { return impactLight.impactOccurred() }
        play([CHHapticEvent(eventType: .hapticTransient, parameters: [
            CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.35),
            CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.9),
        ], relativeTime: 0)])
    }

    /// A drawn zone closed and was accepted into the report.
    public func zoneComplete() {
        guard supportsHaptics else { return impactMedium.impactOccurred() }
        play([CHHapticEvent(eventType: .hapticTransient, parameters: [
            CHHapticEventParameter(parameterID: .hapticIntensity, value: 0.8),
            CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.5),
        ], relativeTime: 0)])
    }

    /// The one the wrapper genuinely cannot do: a short burst whose FEEL encodes
    /// the snow under your finger.
    ///
    /// - Parameters:
    ///   - depthCm: reported/modelled depth. More snow → stronger.
    ///   - crusty:  0 = soft powder, 1 = hard wind-crust or melt-freeze.
    ///              Drives sharpness, so powder feels dull and round while a
    ///              crust feels brittle and sharp.
    public func snowTexture(depthCm: Double, crusty: Double) {
        let intensity = Float(max(0.15, min(1.0, depthCm / 80.0)))
        let sharpness = Float(max(0.0, min(1.0, crusty)))
        guard supportsHaptics else {
            // Coarse three-step approximation of a continuous scale.
            (intensity > 0.66 ? impactHeavy : intensity > 0.33 ? impactMedium : impactLight)
                .impactOccurred()
            return
        }
        play([CHHapticEvent(eventType: .hapticContinuous, parameters: [
            CHHapticEventParameter(parameterID: .hapticIntensity, value: intensity),
            CHHapticEventParameter(parameterID: .hapticSharpness, value: sharpness),
        ], relativeTime: 0, duration: 0.14)])
    }

    /// Call before a burst of taps (e.g. on drag start) so the Taptic Engine is
    /// warm and the first tick is not late.
    public func prepare() {
        selection.prepare()
        impactLight.prepare()
    }
}
