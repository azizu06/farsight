import Foundation

/// Sample the sensor clock on a fixed cadence instead of rounding each gap up
/// to another camera callback. Missed slots are skipped, never queued.
struct CaptureCadence {
    static let depthFPS = 12.0
    static let rgbFPS = 15.0

    private var nextCapture = -Double.infinity

    func isDue(at time: Double) -> Bool {
        time + 0.000001 >= nextCapture
    }

    mutating func didCapture(at time: Double, fps: Double) {
        let interval = 1 / fps
        let phase = nextCapture.isFinite ? nextCapture : time
        let missed = floor(max(0, (time - phase) / interval))
        nextCapture = phase + (missed + 1) * interval
    }
}
