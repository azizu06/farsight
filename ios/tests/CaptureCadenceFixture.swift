import Foundation

@main
enum CaptureCadenceFixture {
    static func samples(_ times: [Double], fps: Double) -> Int {
        var cadence = CaptureCadence()
        return times.reduce(0) { count, time in
            if !cadence.isDue(at: time) { return count }
            cadence.didCapture(at: time, fps: fps)
            return count + 1
        }
    }

    static func main() {
        // Ten seconds of real camera callback rates. A 30 Hz sensor needs
        // alternating 2/3-callback gaps to deliver a 12 Hz sampled stream.
        for cameraFPS in [30.0, 60.0] {
            let times = (0..<Int(cameraFPS * 10)).map { 1000 + Double($0) / cameraFPS }
            precondition(samples(times, fps: CaptureCadence.depthFPS) == 120)
            precondition(samples(times, fps: CaptureCadence.rgbFPS) == 150)
            let jittered = times.enumerated().map { $0.element + Double($0.offset % 3 - 1) * 0.001 }
            precondition(abs(samples(jittered, fps: CaptureCadence.depthFPS) - 120) <= 1)
        }

        let times = (0..<600).map { 1000 + Double($0) / 60 }
        var last = -Double.infinity
        let rounded = times.reduce(0) { count, time in
            if time - last < 1 / CaptureCadence.depthFPS { return count }
            last = time
            return count + 1
        }
        precondition(rounded < samples(times, fps: CaptureCadence.depthFPS))

        // An in-flight send or a late callback loses slots, with no catch-up
        // queue and no second sample of the same sensor timestamp.
        var cadence = CaptureCadence()
        precondition(cadence.isDue(at: 10))
        cadence.didCapture(at: 10, fps: CaptureCadence.depthFPS)
        precondition(!cadence.isDue(at: 10))
        precondition(cadence.isDue(at: 12.06))
        cadence.didCapture(at: 12.06, fps: CaptureCadence.depthFPS)
        precondition(!cadence.isDue(at: 12.07))
        precondition(cadence.isDue(at: 12.084))
        cadence = CaptureCadence()  // reconnect changes the sensor time origin
        precondition(cadence.isDue(at: 0))
        print("PASS capture cadence: 12 RGB-D / 15 RGB fps at 30/60 Hz, jitter, skipped slots, reset; old rounded rule: \(rounded)/120")
    }
}
