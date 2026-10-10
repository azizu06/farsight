import Foundation

/// One explicit capture intent, fresh transport attempts, and bounded backoff.
/// Call only on the capture queue. Tickets never survive Stop or a new Start.
struct CaptureReconnect {
    struct Ticket: Equatable {
        let intent: UUID
        let revision: Int
        let delay: Double
    }
    private(set) var intent: UUID?
    private(set) var attempt: UUID?
    private var failures = 0
    private var revision = 0

    mutating func start(_ intent: UUID) {
        self.intent = intent; attempt = nil; failures = 0
    }
    mutating func stop() { intent = nil; attempt = nil; failures = 0 }
    mutating func beginAttempt(for intent: UUID) -> UUID? {
        guard self.intent == intent, attempt == nil else { return nil }
        let id = UUID(); attempt = id; return id
    }
    mutating func healthy(_ attempt: UUID) {
        guard self.attempt == attempt else { return }
        failures = 0
    }
    mutating func failed(_ attempt: UUID) -> Ticket? {
        guard self.attempt == attempt, let intent else { return nil }
        self.attempt = nil
        failures += 1; revision += 1
        return Ticket(intent: intent, revision: revision,
                      delay: min(8, 0.5 * pow(2, Double(min(failures - 1, 4)))))
    }
    func mayRetry(_ ticket: Ticket) -> Bool {
        intent == ticket.intent && attempt == nil && revision == ticket.revision
    }
}


/// Stop revokes admission immediately, before queued camera/transport cleanup.
/// Hold only across the sensor start or asynchronous socket submission, not encoding.
final class CaptureIntentAdmission {
    private let lock = NSRecursiveLock()
    private var intent: UUID?
    func start(_ intent: UUID) { lock.lock(); self.intent = intent; lock.unlock() }
    func stop() { lock.lock(); intent = nil; lock.unlock() }
    func revoke(_ intent: UUID) {
        lock.lock(); defer { lock.unlock() }
        if self.intent == intent { self.intent = nil }
    }
    func withIntent<T>(_ intent: UUID, _ operation: () -> T) -> T? {
        lock.lock(); defer { lock.unlock() }
        guard self.intent == intent else { return nil }
        return operation()
    }
}
