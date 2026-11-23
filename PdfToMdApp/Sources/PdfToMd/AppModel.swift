// The app's state: the options of each step (saved between launches), the project folder, and the run with its log.

import AppKit
import PdfToMdCore

@MainActor
final class AppModel: ObservableObject {
    enum RunState: Equatable {
        case idle
        case running
        case stopping
        case finished(PipelineOutcome)
    }

    struct LogLine: Identifiable {
        let id: Int
        let text: String
        let kind: RunProgress.LineKind
    }

    /// The step running, or the last one run, and its place in the run.
    struct StepPosition: Equatable {
        let step: PipelineStep
        let number: Int
        let count: Int
    }

    /// The log keeps the last this many lines: Docling's own log can run long.
    static let maxLogLines = 5000
    /// On quitting, how long the script gets to stop before it is terminated.
    static let quitTimeout: TimeInterval = 5
    /// Each step's options are saved under a key of their own, so that a change to one never resets the others.
    private static let optionsKey = "options"
    private static let chunkOptionsKey = "chunkOptions"
    private static let embedOptionsKey = "embedOptions"
    private static let projectRootKey = "projectRoot"

    @Published var options: ScriptOptions {
        didSet {
            save(options, as: Self.optionsKey)
            if options.inputDirectory != oldValue.inputDirectory {
                refreshDocuments()
            }
        }
    }
    @Published var chunkOptions: ChunkOptions {
        didSet {
            if !chunkOptions.enabled {
                embedOptions.enabled = false  // embedding reads the chunks of the same run
            }
            save(chunkOptions, as: Self.chunkOptionsKey)
        }
    }
    /// The PDFs and Word files of the input folder, and the names chunk-config.json has settings for (see refreshDocuments).
    @Published private(set) var documentFiles: [DocumentFile] = []
    @Published private(set) var configuredDocuments: Set<String> = []
    @Published private(set) var documentConfigProblem: String?
    @Published var embedOptions: EmbedOptions {
        didSet { save(embedOptions, as: Self.embedOptionsKey) }
    }
    @Published private(set) var projectRoot: URL?
    @Published private(set) var projectProblem: String?
    @Published private(set) var log: [LogLine] = []
    @Published private(set) var progress = RunProgress()
    @Published private(set) var state = RunState.idle
    @Published private(set) var position: StepPosition?
    private var runner: PipelineRunner?
    private var nextLineID = 0

    init() {
        options = Self.load(ScriptOptions.self, key: Self.optionsKey) ?? ScriptOptions()
        chunkOptions = Self.load(ChunkOptions.self, key: Self.chunkOptionsKey) ?? ChunkOptions()
        embedOptions = Self.load(EmbedOptions.self, key: Self.embedOptionsKey) ?? EmbedOptions()
        let saved = UserDefaults.standard.string(forKey: Self.projectRootKey).map { URL(fileURLWithPath: $0) }
        projectRoot = saved.flatMap { ProjectLocator.isProject($0) ? $0 : nil }
            ?? ProjectLocator.find(from: [Bundle.main.bundleURL, URL(fileURLWithPath: FileManager.default.currentDirectoryPath)])
        if projectRoot == nil {
            projectProblem = "No folder with \(ProjectLocator.scriptName) and \(ProjectLocator.pythonRelativePath) was found."
        }
        refreshDocuments()
    }

    /// The input PDFs that chunk-config.json has no settings for: chunking would fail on them.
    var documentsWithoutSettings: [DocumentFile] {
        documentFiles.filter { !configuredDocuments.contains($0.key) }
    }

    /// Reads the input folder and chunk-config.json again: after the settings sheet, when the input folder changes,
    /// when the app comes back to the front (files may have been added), and before a run.
    func refreshDocuments() {
        documentFiles = options.inputDirectory.isEmpty ? [] : DocumentFiles.files(in: URL(fileURLWithPath: options.inputDirectory))
        guard let projectRoot else {
            return
        }
        do {
            let entries = try DocumentConfigFile.load(from: DocumentConfigFile.url(in: projectRoot))
            configuredDocuments = Set(entries.keys)
            documentConfigProblem = DocumentConfigFile.problems(in: entries).first.map { "\(DocumentConfigFile.fileName): \($0)" }
        } catch {
            configuredDocuments = []
            documentConfigProblem = error.localizedDescription
        }
    }

    var isRunning: Bool { state == .running || state == .stopping }

    var pipeline: PipelineOptions {
        PipelineOptions(sources: Set(documentFiles.map(\.kind)), ocr: options, chunk: chunkOptions, embed: embedOptions)
    }

    /// The commands the options make, or why they make none.
    var runs: Result<[PipelineRun], Error>? {
        guard let projectRoot else {
            return nil
        }
        return Result { try pipeline.runs(projectRoot: projectRoot) }
    }

    /// What stops a run, in plain words.
    var problems: [String] {
        (projectRoot == nil ? ["Choose the project folder."] : []) + pipeline.problems() + inputProblems + documentProblems
    }

    /// An input folder with nothing to convert, or with two files that would write the same results.
    private var inputProblems: [String] {
        var isFolder: ObjCBool = false
        let folderExists = FileManager.default.fileExists(atPath: options.inputDirectory, isDirectory: &isFolder) && isFolder.boolValue
        if folderExists && documentFiles.isEmpty {
            return ["No PDFs or Word files in the input folder."]
        }
        let nameless = DocumentFiles.nameless(in: documentFiles).map { "\"\($0.name)\" has no name to write its results under: rename it." }
        return nameless + DocumentFiles.clashes(in: documentFiles).map { clash in
            "\(clash.map(\.name).joined(separator: " and ")) would write the same results: keep one."
        }
    }

    /// Chunking fails on a document without settings, after OCR has taken its time: say so before the run.
    private var documentProblems: [String] {
        guard pipeline.runsChunking else {
            return []
        }
        if let documentConfigProblem {
            return [documentConfigProblem]
        }
        let missing = documentsWithoutSettings.map(\.name)
        return missing.isEmpty ? [] : ["No document settings for \(missing.joined(separator: ", ")): set them under Chunking."]
    }

    var canStart: Bool { !isRunning && problems.isEmpty }

    func chooseProjectFolder() {
        guard let folder = pickFolder(message: "The project folder, with \(ProjectLocator.scriptName) and venvs/", from: projectRoot?.path) else {
            return
        }
        guard ProjectLocator.isProject(folder) else {
            let alert = NSAlert()
            alert.messageText = "Not a project folder."
            alert.informativeText = "\(folder.path) has no \(ProjectLocator.scriptName) and \(ProjectLocator.pythonRelativePath)."
            alert.runModal()
            return
        }
        projectRoot = folder
        projectProblem = nil
        UserDefaults.standard.set(folder.path, forKey: Self.projectRootKey)
        refreshDocuments()
    }

    /// Lets the user pick a folder for one of the options' paths.
    func chooseFolder(_ path: ReferenceWritableKeyPath<AppModel, String>, message: String, canCreate: Bool = true) {
        if let folder = pickFolder(message: message, from: self[keyPath: path], canCreate: canCreate) {
            self[keyPath: path] = folder.path
        }
    }

    func start() {
        refreshDocuments()
        guard canStart, case .success(let runs)? = runs else {
            return
        }
        log = []
        nextLineID = 0
        progress = RunProgress()
        position = nil
        let runner = PipelineRunner(
            runs: runs,
            environment: ChildEnvironment.make(from: ProcessInfo.processInfo.environment),
            onStepStarted: { [weak self] run, index in self?.stepStarted(run, index: index, count: runs.count) },
            onOutput: { [weak self] lines, progress in self?.append(lines, progress: progress) },
            onExit: { [weak self] outcome in self?.finish(outcome) }
        )
        self.runner = runner
        state = .running
        runner.start()
    }

    func stop() {
        guard state == .running else {
            return
        }
        state = .stopping
        runner?.stop()
    }

    func stopForQuitting() {
        runner?.stopAndWait(timeout: Self.quitTimeout)
    }

    func showOutputFolder() {
        let folder = URL(fileURLWithPath: options.outputDirectory)
        if FileManager.default.fileExists(atPath: folder.path) {
            NSWorkspace.shared.open(folder)
        }
    }

    func copyCommands() {
        guard case .success(let runs)? = runs else {
            return
        }
        copy(runs.map(\.invocation.commandLine).joined(separator: "\n"))
    }

    func copyLog() {
        copy(log.map(\.text).joined(separator: "\n"))
    }

    private func stepStarted(_ run: PipelineRun, index: Int, count: Int) {
        position = StepPosition(step: run.step, number: index + 1, count: count)
        progress = RunProgress()
        append([OutputLine(text: "$ " + run.invocation.commandLine, kind: .other)], progress: progress)
    }

    private func append(_ lines: [OutputLine], progress: RunProgress) {
        for line in lines {
            log.append(LogLine(id: nextLineID, text: line.text, kind: line.kind))
            nextLineID += 1
        }
        if log.count > Self.maxLogLines {
            log.removeFirst(log.count - Self.maxLogLines)
        }
        self.progress = progress
    }

    private func finish(_ outcome: PipelineOutcome) {
        runner = nil
        state = .finished(outcome)
    }

    private static func load<Options: Decodable>(_ type: Options.Type, key: String) -> Options? {
        UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(type, from: $0) }
    }

    private func save<Options: Encodable>(_ options: Options, as key: String) {
        if let data = try? JSONEncoder().encode(options) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private func pickFolder(message: String, from path: String?, canCreate: Bool = false) -> URL? {
        let panel = NSOpenPanel()
        panel.message = message
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = canCreate
        if let path, !path.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: path)
        }
        return panel.runModal() == .OK ? panel.url : nil
    }
}
