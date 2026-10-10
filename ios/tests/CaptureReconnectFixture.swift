import Foundation

@main struct CaptureReconnectFixture {
    static func main() {
        var recovery = CaptureReconnect()
        let intent = UUID(); recovery.start(intent)
        var attempts = Set<UUID>(), tickets = [CaptureReconnect.Ticket]()
        for index in 0..<12 {
            let attempt = recovery.beginAttempt(for: intent)!
            precondition(attempts.insert(attempt).inserted, "each transport attempt is fresh")
            precondition(recovery.beginAttempt(for: intent) == nil, "only one attempt can run")
            let ticket = recovery.failed(attempt)!
            precondition(recovery.failed(attempt) == nil, "duplicate callback cannot schedule another retry")
            precondition(ticket.delay == min(8, 0.5 * pow(2, Double(min(index, 4)))))
            precondition(recovery.mayRetry(ticket))
            for old in tickets { precondition(!recovery.mayRetry(old), "old timer cannot revive an attempt") }
            tickets.append(ticket)
        }
        let healthy = recovery.beginAttempt(for: intent)!
        recovery.healthy(attempts.first!) // stale frame completion cannot reset backoff
        let afterStale = recovery.failed(healthy)!
        precondition(afterStale.delay == 8)
        let fresh = recovery.beginAttempt(for: intent)!
        recovery.healthy(fresh)
        let afterSuccess = recovery.failed(fresh)!
        precondition(afterSuccess.delay == 0.5, "a successful fresh frame resets backoff")
        for old in tickets { precondition(!recovery.mayRetry(old), "healthy reset cannot revive an old ticket") }
        recovery.stop()
        precondition(!recovery.mayRetry(afterSuccess))
        precondition(recovery.beginAttempt(for: intent) == nil)
        precondition(recovery.failed(fresh) == nil)
        let newer = UUID(); recovery.start(newer)
        precondition(!recovery.mayRetry(afterSuccess), "Stop/Start discards old tickets")
        precondition(recovery.beginAttempt(for: intent) == nil)
        precondition(recovery.beginAttempt(for: newer) != nil)
        let admission = CaptureIntentAdmission()
        admission.start(intent)
        precondition(admission.withIntent(intent) { true } == true)
        admission.stop()
        precondition(admission.withIntent(intent) { fatalError("old queued send admitted after Stop") } == nil)
        admission.start(newer)
        admission.revoke(intent) // old camera failure cannot revoke a newer Start
        precondition(admission.withIntent(intent) { true } == nil)
        precondition(admission.withIntent(newer) { true } == true)
        // Model encoding which finishes after Stop: it must never submit its packet.
        let started = DispatchSemaphore(value: 0), finish = DispatchSemaphore(value: 0), done = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            started.signal(); finish.wait()
            precondition(admission.withIntent(newer) { fatalError("late encoded frame sent") } == nil)
            done.signal()
        }
        started.wait(); admission.stop(); finish.signal()
        precondition(done.wait(timeout: .now() + 2) == .success)
        print("PASS capture recovery: fresh attempts, capped delays, stale callbacks, Stop and new Start")
    }
}
