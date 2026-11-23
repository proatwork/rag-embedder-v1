// Where the project and its Python are, and the command and environment a run gets.

import Foundation

public enum ProjectLocator {
    public static let scriptName = PipelineStep.ocr.scriptName
    /// The interpreter Docling is installed in, as MANUAL.md runs the scripts.
    public static let pythonRelativePath = "venvs/bin/python"
    static let maxLevelsUp = 8

    public static func isProject(_ folder: URL) -> Bool {
        let fileManager = FileManager.default
        return fileManager.fileExists(atPath: folder.appendingPathComponent(scriptName).path)
            && fileManager.isExecutableFile(atPath: folder.appendingPathComponent(pythonRelativePath).path)
    }

    /// The first project folder among `starts` and the folders above each (the app sits in PdfToMdApp/ inside it).
    public static func find(from starts: [URL]) -> URL? {
        for start in starts {
            var folder = start.standardizedFileURL
            for _ in 0..<maxLevelsUp {
                if isProject(folder) {
                    return folder
                }
                let parent = folder.deletingLastPathComponent().standardizedFileURL
                if parent.path == folder.path {
                    break
                }
                folder = parent
            }
        }
        return nil
    }
}

/// One run of a script: the project's Python, with the script's name and its arguments, in the project folder.
public struct Invocation: Equatable, Sendable {
    public let executable: URL
    public let arguments: [String]
    public let workingDirectory: URL

    public init(projectRoot: URL, script: String, arguments: [String]) {
        executable = projectRoot.appendingPathComponent(ProjectLocator.pythonRelativePath)
        self.arguments = [script] + arguments
        workingDirectory = projectRoot
    }

    /// The conversion script for `source` (pdf_to_md.py or docx_to_md.py) with these options.
    public init(projectRoot: URL, options: ScriptOptions, source: SourceKind = .pdf) throws {
        self.init(
            projectRoot: projectRoot, script: source.conversionStep.scriptName,
            arguments: try options.scriptArguments(for: source)
        )
    }

    /// The same command as typed in a terminal in the project folder.
    public var commandLine: String {
        ShellQuoting.commandLine([ProjectLocator.pythonRelativePath] + arguments)
    }
}

public enum ChildEnvironment {
    /// Homebrew's folders, where `tesseract` is: an app opened from the Finder gets a PATH without them, and Docling's
    /// tesseract engine runs the `tesseract` command.
    public static let extraPathFolders = ["/opt/homebrew/bin", "/usr/local/bin"]
    /// What macOS gives an app with no PATH at all.
    static let systemPath = "/usr/bin:/bin:/usr/sbin:/sbin"
    static let pathKey = "PATH"
    static let pathSeparator: Character = ":"
    /// Python's output unbuffered and in UTF-8, whatever the app's own environment says.
    static let pythonOutputSettings = ["PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"]

    /// `base` (the app's own environment) with Homebrew's folders on the PATH and Python's output unbuffered, so that
    /// the script's lines and Docling's log arrive as they are written.
    public static func make(from base: [String: String]) -> [String: String] {
        var environment = base
        let path = base[pathKey].flatMap { $0.isEmpty ? nil : $0 } ?? systemPath
        var folders = path.split(separator: pathSeparator).map(String.init)
        folders += extraPathFolders.filter { !folders.contains($0) }
        environment[pathKey] = folders.joined(separator: String(pathSeparator))
        environment.merge(pythonOutputSettings) { _, setting in setting }
        return environment
    }
}
