// Tests of the parts that need no process: the command line the options make, reading the output, the environment,
// and finding the project.

import Foundation
import PdfToMdCore
import XCTest

private func temporaryFolder() throws -> URL {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    return folder
}

final class ScriptOptionsTests: XCTestCase {
    private func options(_ change: (inout ScriptOptions) -> Void = { _ in }) -> ScriptOptions {
        var options = ScriptOptions()
        options.inputDirectory = "/in"
        options.outputDirectory = "/out"
        change(&options)
        return options
    }

    func testDefaultsAreTheScriptDefaults() throws {
        XCTAssertEqual(try options().scriptArguments(), [
            "--input-dir", "/in", "--output-dir", "/out", "--to", "md",
            "--ocr-engine", "tesseract", "--ocr-lang", "ron", "--ocr-mode", "full_page", "--device", "auto",
        ])
    }

    func testEveryOptionReachesTheCommandLine() throws {
        let arguments = try options {
            $0.skipExisting = true
            $0.format = .json
            $0.images = true
            $0.pageImages = true
            $0.ocrEngine = "ocrmac"
            $0.ocrMode = .layoutRegions
            $0.device = .mps
            $0.extraDoclingOptions = "--page-range 1-4"
        }.scriptArguments()
        XCTAssertEqual(arguments, [
            "--input-dir", "/in", "--output-dir", "/out", "--skip-existing", "--to", "json", "--images", "--page-images",
            "--ocr-engine", "ocrmac", "--ocr-lang", "ro-RO", "--ocr-mode", "layout_regions", "--device", "mps",
            "--", "--page-range", "1-4",
        ])
    }

    func testWordFilesTakeNoOCROptions() throws {
        let arguments = try options {
            $0.format = .json
            $0.images = true
            $0.extraDoclingOptions = "--page-range 1-4"
        }.scriptArguments(for: .docx)
        XCTAssertEqual(arguments, [
            "--input-dir", "/in", "--output-dir", "/out", "--to", "json", "--images", "--", "--page-range", "1-4",
        ])
    }

    func testTheOptionsTheFormSetsAreRefusedOnlyWherePDFsAreConverted() throws {
        let deviceAfterDashes = options {
            $0.inputDirectory = "/"  // a folder that exists, so that only the extra options are judged
            $0.extraDoclingOptions = "--device cpu"
        }
        XCTAssertEqual(Array(try deviceAfterDashes.scriptArguments(for: .docx).suffix(3)), ["--", "--device", "cpu"])
        XCTAssertThrowsError(try deviceAfterDashes.scriptArguments(for: .pdf))
        XCTAssertEqual(deviceAfterDashes.problems(refusingOCROptions: false), [])
        XCTAssertEqual(deviceAfterDashes.problems(), [OptionsProblem.optionSetAbove("--device").localizedDescription])
    }

    func testPicturesNeedJSONAndPageImagesNeedPictures() {
        var options = options { $0.format = .json }
        options.images = true
        options.pageImages = true
        options.format = .markdown
        XCTAssertFalse(options.images)
        XCTAssertFalse(options.pageImages)

        options.format = .json
        options.images = true
        options.pageImages = true
        options.images = false
        XCTAssertFalse(options.pageImages)
    }

    func testSavedPictureFlagsTheScriptWouldRefuseAreNotSent() throws {
        // A saved set decodes without the didSet rules running; the command line still follows the script's rules.
        func arguments(format: String, images: Bool) throws -> [String] {
            let saved = #"{"inputDirectory":"/in","outputDirectory":"/out","skipExisting":false,"format":"\#(format)","images":\#(images),"pageImages":true,"ocrEngine":"tesseract","ocrLanguage":"ron","ocrMode":"full_page","device":"auto","extraDoclingOptions":""}"#
            return try JSONDecoder().decode(ScriptOptions.self, from: Data(saved.utf8)).scriptArguments()
        }
        let markdown = try arguments(format: "md", images: true)
        XCTAssertFalse(markdown.contains("--images"))
        XCTAssertFalse(markdown.contains("--page-images"))
        XCTAssertFalse(try arguments(format: "json", images: false).contains("--page-images"))
    }

    func testOptionsSurviveSaving() throws {
        let original = options {
            $0.format = .json
            $0.images = true
            $0.ocrEngine = "ocrmac"
            $0.ocrLanguage = "en-US"
        }
        let decoded = try JSONDecoder().decode(ScriptOptions.self, from: JSONEncoder().encode(original))
        XCTAssertEqual(decoded, original)
    }

    func testAnotherEngineGetsItsOwnLanguage() {
        var options = options()
        options.ocrEngine = "ocrmac"
        XCTAssertEqual(options.ocrLanguage, "ro-RO")
        options.ocrLanguage = "en-US"
        options.ocrEngine = "ocrmac"  // the same engine again keeps what was typed
        XCTAssertEqual(options.ocrLanguage, "en-US")
        options.ocrEngine = "rapidocr"
        XCTAssertEqual(options.ocrLanguage, "")
        options.ocrEngine = "tesseract"
        XCTAssertEqual(options.ocrLanguage, "ron")
    }

    func testOptionsTheScriptSetsAreRefused() {
        for typed in ["--to json", "--to=json", "--output x", "--output-file x.md", "--image-export-mode embedded"] {
            let option = String(typed.split(whereSeparator: { $0 == " " || $0 == "=" })[0])
            XCTAssertThrowsError(try options { $0.extraDoclingOptions = typed }.scriptArguments(), typed) {
                XCTAssertEqual($0 as? OptionsProblem, .reservedDoclingOption(option))
            }
        }
    }

    func testOptionsTheFormSetsAreRefused() {
        XCTAssertThrowsError(try options { $0.extraDoclingOptions = "--psm 6 --device=cpu" }.scriptArguments()) {
            XCTAssertEqual($0 as? OptionsProblem, .optionSetAbove("--device"))
        }
    }

    func testLookalikeOptionsAreKept() throws {
        let arguments = try options { $0.extraDoclingOptions = "--toc --outputs" }.scriptArguments()
        XCTAssertEqual(Array(arguments.suffix(3)), ["--", "--toc", "--outputs"])
    }

    func testProblems() throws {
        XCTAssertEqual(ScriptOptions().problems(), [
            "Choose the folder with the PDFs or Word files.", "Choose the folder the results go to.",
        ])
        let missing = options { $0.inputDirectory = "/no/such/folder" }
        XCTAssertEqual(missing.problems(), ["The input is not a folder: /no/such/folder"])

        let folder = try temporaryFolder()
        defer { try? FileManager.default.removeItem(at: folder) }
        XCTAssertEqual(options { $0.inputDirectory = folder.path }.problems(), [])
        let file = folder.appendingPathComponent("a.pdf")
        FileManager.default.createFile(atPath: file.path, contents: Data())
        XCTAssertEqual(options { $0.inputDirectory = file.path }.problems(), ["The input is not a folder: \(file.path)"])
        let unclosed = options {
            $0.inputDirectory = folder.path
            $0.extraDoclingOptions = "--page-range '1-4"
        }
        XCTAssertEqual(unclosed.problems(), [OptionsProblem.unclosedQuote.localizedDescription])
    }
}

final class ArgumentSplitterTests: XCTestCase {
    func testSplitting() throws {
        let cases: [(String, [String])] = [
            ("", []),
            ("   ", []),
            ("--page-range 1-4", ["--page-range", "1-4"]),
            ("  a \t b  ", ["a", "b"]),
            ("--pdf-password 'two words'", ["--pdf-password", "two words"]),
            (#"--pdf-password "say \"hi\" \\ \n""#, ["--pdf-password", #"say "hi" \ \n"#]),
            (#"a\ b"#, ["a b"]),
            ("''", [""]),
            ("x''y", ["xy"]),
            (#"'it'\''s'"#, ["it's"]),
            (#"end\"#, [#"end\"#]),
        ]
        for (text, words) in cases {
            XCTAssertEqual(try ArgumentSplitter.split(text), words, text)
        }
    }

    func testUnclosedQuote() {
        for text in ["'a", #""a"#, #""a\""#] {
            XCTAssertThrowsError(try ArgumentSplitter.split(text), text) {
                XCTAssertEqual($0 as? OptionsProblem, .unclosedQuote)
            }
        }
    }

    func testQuotedCommandLinesReadBack() throws {
        let words = ["plain", "", "two words", "it's", #"back\slash"#, "/Users/x/Normative ISU/ăîșțâ", "a=b", "$HOME"]
        XCTAssertEqual(try ArgumentSplitter.split(ShellQuoting.commandLine(words)), words)
        XCTAssertEqual(ShellQuoting.quote("--page-range"), "--page-range")
        XCTAssertEqual(ShellQuoting.quote("two words"), "'two words'")
    }
}

final class LineSplitterTests: XCTestCase {
    func testALetterSplitAcrossReadsStaysWhole() {
        var splitter = LineSplitter()
        let bytes = Array("== [1/1] Normativ_ș.pdf\n".utf8)
        let cut = bytes.firstIndex(of: 0xC8)! + 1  // inside the two bytes of "ș"
        XCTAssertEqual(splitter.feed(Data(bytes[..<cut])), [])
        XCTAssertEqual(splitter.feed(Data(bytes[cut...])), ["== [1/1] Normativ_ș.pdf"])
    }

    func testLineEnds() {
        var splitter = LineSplitter()
        XCTAssertEqual(splitter.feed(Data("a\nb\r\nc\rd\r".utf8)), ["a", "b", "c", "d"])
        XCTAssertEqual(splitter.feed(Data("\ne\n\nf".utf8)), ["e", ""])  // the "\n" of a "\r\n" cut in two
        XCTAssertEqual(splitter.finish(), ["f"])
        XCTAssertEqual(splitter.finish(), [])
    }
}

final class RunProgressTests: XCTestCase {
    func testStatusLines() {
        var progress = RunProgress()
        XCTAssertEqual(progress.read("== [1/3] Normativ P118-2.pdf  ->  /out/Normativ P118-2.md"), .fileStarted)
        XCTAssertEqual(progress.fileNumber, 1)
        XCTAssertEqual(progress.fileCount, 3)
        XCTAssertEqual(progress.currentFile, "Normativ P118-2.pdf")
        XCTAssertEqual(progress.read("2026-11-21 10:00:00,000 - INFO - Processing document"), .other)
        XCTAssertEqual(progress.read("   ok in 31s"), .converted)
        XCTAssertEqual(progress.read("== [2/3] b.pdf  ->  /out/b.md"), .fileStarted)
        XCTAssertEqual(progress.read("   skipped (output exists)"), .skipped)
        XCTAssertEqual(progress.read("== [3/3] c.pdf  ->  /out/c.md"), .fileStarted)
        XCTAssertEqual(progress.read("   FAILED (docling exited with status 1) in 3s"), .failed)
        XCTAssertEqual(progress.read("failed: c.pdf"), .other)
        XCTAssertEqual(progress.converted, 1)
        XCTAssertEqual(progress.skipped, 1)
        XCTAssertEqual(progress.failedFiles, ["c.pdf"])
        XCTAssertEqual(progress.finishedCount, 3)
    }

    func testTheScriptsOtherStatusLines() {
        var progress = RunProgress()
        XCTAssertEqual(progress.read("   FAILED (the output would overwrite the source)"), .failed)  // before any file line
        XCTAssertEqual(progress.read("== [1/2] a.docx  ->  /out/a.json"), .fileStarted)
        XCTAssertEqual(progress.read("   ok in 12s (3 pictures)"), .converted)
        XCTAssertEqual(progress.failedFiles, [""])
        XCTAssertEqual(progress.converted, 1)
    }

    func testLinesThatOnlyLookLikeAFileStart() {
        var progress = RunProgress()
        for line in ["== [x/3] a.pdf  ->  b", "== [1] a.pdf", "== [1/3 a.pdf", "[1/3] a.pdf"] {
            XCTAssertEqual(progress.read(line), .other, line)
        }
        XCTAssertNil(progress.fileCount)
    }
}

final class ChildEnvironmentTests: XCTestCase {
    func testTheFinderPathGetsHomebrew() {
        let environment = ChildEnvironment.make(from: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/Users/x"])
        XCTAssertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin")
        XCTAssertEqual(environment["HOME"], "/Users/x")
        XCTAssertEqual(environment["PYTHONUNBUFFERED"], "1")
        XCTAssertEqual(environment["PYTHONIOENCODING"], "utf-8")
    }

    func testAPathWithHomebrewIsKeptAsItIs() {
        let path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin"
        XCTAssertEqual(ChildEnvironment.make(from: ["PATH": path])["PATH"], path)
    }

    func testNoPathAtAll() {
        XCTAssertEqual(
            ChildEnvironment.make(from: [:])["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        )
    }
}

final class ProjectLocatorTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = try temporaryFolder()
    }

    override func tearDownWithError() throws {
        try FileManager.default.removeItem(at: root)
    }

    private func makeProject(at folder: URL) throws {
        let python = folder.appendingPathComponent(ProjectLocator.pythonRelativePath)
        try FileManager.default.createDirectory(at: python.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: python.path, contents: Data(), attributes: [.posixPermissions: 0o755])
        FileManager.default.createFile(atPath: folder.appendingPathComponent(ProjectLocator.scriptName).path, contents: Data())
    }

    func testFoundFromInsideTheApp() throws {
        try makeProject(at: root)
        let app = root.appendingPathComponent("PdfToMdApp/PdfToMd.app/Contents/MacOS")
        try FileManager.default.createDirectory(at: app, withIntermediateDirectories: true)
        XCTAssertEqual(ProjectLocator.find(from: [app])?.path, root.standardizedFileURL.path)
    }

    func testThePythonMustBeThere() throws {
        FileManager.default.createFile(atPath: root.appendingPathComponent(ProjectLocator.scriptName).path, contents: Data())
        XCTAssertFalse(ProjectLocator.isProject(root))
        XCTAssertNil(ProjectLocator.find(from: [root]))
    }

    func testTheInvocationRunsInTheProject() throws {
        var options = ScriptOptions()
        options.inputDirectory = "/in put"
        options.outputDirectory = "/out"
        let invocation = try Invocation(projectRoot: root, options: options)
        XCTAssertEqual(invocation.executable, root.appendingPathComponent("venvs/bin/python"))
        XCTAssertEqual(invocation.workingDirectory, root)
        XCTAssertEqual(Array(invocation.arguments.prefix(3)), ["pdf_to_md.py", "--input-dir", "/in put"])
        XCTAssertTrue(invocation.commandLine.hasPrefix("venvs/bin/python pdf_to_md.py --input-dir '/in put' --output-dir /out"))
    }
}
