// Runs the steps of a pipeline one after another, each with its own ScriptRunner.

import Foundation

public enum PipelineOutcome: Equatable, Sendable {
    case succeeded
    /// Every step ran; in these some files failed, and the others went on to the next step.
    case finishedWithFailures([PipelineStep])
    case stopped(PipelineStep)
    /// This step produced nothing, or ended by a signal; the steps after it did not run.
    case failed(PipelineStep, RunOutcome)
    case couldNotStart(PipelineStep, String)
}

public struct OutputLine: Equatable, Sendable {
    public let text: String
    public let kind: RunProgress.LineKind

    public init(text: String, kind: RunProgress.LineKind) {
        self.text = text
        self.kind = kind
    }
}

/// Use it on `callbackQueue` (the main queue by default), where its callbacks also arrive.
public final class PipelineRunner {
    private let runs: [PipelineRun]
    private let environment: [String: String]
    private let stopGracePeriod: TimeInterval
    private let callbackQueue: DispatchQueue
    private let onStepStarted: (PipelineRun, Int) -> Void
    private let onOutput: ([OutputLine], RunProgress) -> Void
    private let onExit: (PipelineOutcome) -> Void

    private var current: ScriptRunner?
    private var index = 0
    private var progress = RunProgress()
    private var stepsWithFailures: [PipelineStep] = []
    private var stopRequested = false

    /// `onStepStarted` gets each run and its index as it starts, `onOutput` the step's lines with the progress so far,
    /// and `onExit` how the pipeline ended, once.
    public init(
        runs: [PipelineRun],
        environment: [String: String],
        stopGracePeriod: TimeInterval = ScriptRunner.defaultStopGracePeriod,
        callbackQueue: DispatchQueue = .main,
        onStepStarted: @escaping (PipelineRun, Int) -> Void,
        onOutput: @escaping ([OutputLine], RunProgress) -> Void,
        onExit: @escaping (PipelineOutcome) -> Void
    ) {
        precondition(!runs.isEmpty, "a pipeline has at least its OCR step")
        self.runs = runs
        self.environment = environment
        self.stopGracePeriod = stopGracePeriod
        self.callbackQueue = callbackQueue
        self.onStepStarted = onStepStarted
        self.onOutput = onOutput
        self.onExit = onExit
    }

    public func start() {
        startStep(at: 0)
    }

    /// Stops the running step (see ScriptRunner.stop); no later step starts.
    public func stop() {
        stopRequested = true
        current?.stop()
    }

    /// For quitting the app: see ScriptRunner.stopAndWait.
    public func stopAndWait(timeout: TimeInterval) {
        stopRequested = true
        current?.stopAndWait(timeout: timeout)
    }

    private func startStep(at newIndex: Int) {
        index = newIndex
        progress = RunProgress()
        let run = runs[newIndex]
        let runner = ScriptRunner(
            invocation: run.invocation,
            environment: environment,
            stopGracePeriod: stopGracePeriod,
            callbackQueue: callbackQueue,
            onLines: { [weak self] lines in self?.receive(lines) },
            onExit: { [weak self] outcome in self?.stepEnded(outcome) }
        )
        onStepStarted(run, newIndex)
        do {
            try runner.start()
            current = runner
        } catch {
            let reason = error.localizedDescription
            callbackQueue.async { [onExit] in
                onExit(.couldNotStart(run.step, reason))
            }
        }
    }

    private func receive(_ lines: [String]) {
        let output = lines.map { OutputLine(text: $0, kind: progress.read($0)) }
        onOutput(output, progress)
    }

    private func stepEnded(_ outcome: RunOutcome) {
        current = nil
        let step = runs[index].step
        switch outcome {
        case .succeeded:
            break
        case .stopped:
            onExit(.stopped(step))
            return
        case .failed where progress.converted + progress.skipped > 0:
            // run_batch exits 1 when any one file failed: the files that made it still go on, or one bad PDF would
            // hold back every later step on every run.
            stepsWithFailures.append(step)
        case .failed, .killed:
            onExit(.failed(step, outcome))
            return
        }
        guard index + 1 < runs.count else {
            onExit(stepsWithFailures.isEmpty ? .succeeded : .finishedWithFailures(stepsWithFailures))
            return
        }
        guard !stopRequested else {  // Stop came as this step was ending
            onExit(.stopped(step))
            return
        }
        startStep(at: index + 1)
    }
}
