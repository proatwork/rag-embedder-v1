// Tests of the steps after OCR: the commands they make, and PipelineRunner running fake scripts in a scratch project.

import Foundation
import PdfToMdCore
import XCTest

final class PipelineOptionsTests: XCTestCase {
    private let root = URL(fileURLWithPath: "/project")

    /// OCR to JSON into /json, chunks into /chunks, vectors into /vectors; `change` sets the rest.
    private func options(_ change: (inout PipelineOptions) -> Void = { _ in }) -> PipelineOptions {
        var ocr = ScriptOptions()
        ocr.inputDirectory = "/"  // a folder that exists, for ScriptOptions.problems
        ocr.outputDirectory = "/json"
        ocr.format = .json
        var chunk = ChunkOptions()
        chunk.outputDirectory = "/chunks"
        var embed = EmbedOptions()
        embed.outputDirectory = "/vectors"
        var options = PipelineOptions(ocr: ocr, chunk: chunk, embed: embed)
        change(&options)
        return options
    }

    private func steps(_ options: PipelineOptions) throws -> [PipelineStep] {
        try options.runs(projectRoot: root).map(\.step)
    }

    func testOnlyOCRUnlessChunkingIsOn() throws {
        XCTAssertEqual(try steps(options()), [.ocr])
        XCTAssertEqual(try steps(options { $0.embed.enabled = true }), [.ocr], "embedding without chunking")
    }

    func testMarkdownNeverGoesFurther() throws {
        let markdown = options {
            $0.ocr.format = .markdown
            $0.chunk.enabled = true
            $0.embed.enabled = true
        }
        XCTAssertFalse(markdown.chunkingAllowed)
        XCTAssertFalse(markdown.embeddingAllowed)
        XCTAssertEqual(try steps(markdown), [.ocr])
    }

    func testEachStepReadsTheFolderTheStepBeforeWrote() throws {
        let runs = try options {
            $0.chunk.enabled = true
            $0.embed.enabled = true
        }.runs(projectRoot: root)
        XCTAssertEqual(runs.map(\.step), [.ocr, .chunk, .embed])
        XCTAssertEqual(runs[1].invocation.arguments, [
            "json_to_chunks.py", "--input-dir", "/json", "--output-dir", "/chunks",
            "--tokenizer", "BAAI/bge-m3", "--min-tokens", "80", "--max-tokens", "480",
        ])
        XCTAssertEqual(runs[2].invocation.arguments, [
            "embed_chunks.py", "--input-dir", "/chunks", "--output-dir", "/vectors",
        ])
        XCTAssertEqual(runs[2].invocation.commandLine, "venvs/bin/python embed_chunks.py --input-dir /chunks --output-dir /vectors")
        XCTAssertEqual(runs[2].invocation.workingDirectory, root)
    }

    func testWordFilesAreReadAndTheirNumbersPutBack() throws {
        let runs = try options {
            $0.sources = [.docx]
            $0.ocr.inputDirectory = "/word"
            $0.chunk.enabled = true
            $0.embed.enabled = true
        }.runs(projectRoot: root)
        XCTAssertEqual(runs.map(\.step), [.word, .chunk, .embed])
        XCTAssertEqual(runs[0].invocation.arguments, [
            "docx_to_md.py", "--input-dir", "/word", "--output-dir", "/json", "--to", "json",
        ])
        XCTAssertEqual(runs[1].invocation.arguments, [
            "json_to_chunks.py", "--input-dir", "/json", "--output-dir", "/chunks", "--docx-dir", "/word",
            "--tokenizer", "BAAI/bge-m3", "--min-tokens", "80", "--max-tokens", "480",
        ])
        // json_to_chunks.py stops on a --docx-dir without Word files.
        XCTAssertFalse(try options { $0.chunk.enabled = true }.runs(projectRoot: root)[1].invocation.arguments.contains("--docx-dir"), "not for PDFs alone")
    }

    func testAFolderWithBothGetsBothConversionsIntoOneResultsFolder() throws {
        let both = options {
            $0.sources = [.docx, .pdf]
            $0.ocr.inputDirectory = "/mixed"
            $0.chunk.enabled = true
        }
        let runs = try both.runs(projectRoot: root)
        XCTAssertEqual(runs.map(\.step), [.ocr, .word, .chunk])
        XCTAssertEqual(runs[0].invocation.arguments[0], "pdf_to_md.py")
        XCTAssertTrue(runs[0].invocation.arguments.contains("--ocr-engine"))
        XCTAssertEqual(runs[1].invocation.arguments, [
            "docx_to_md.py", "--input-dir", "/mixed", "--output-dir", "/json", "--to", "json",
        ])
        XCTAssertTrue(runs[2].invocation.arguments.contains("--docx-dir"))
        // No files known yet: the PDF command is still shown.
        XCTAssertEqual(try options { $0.sources = [] }.runs(projectRoot: root).map(\.step), [.ocr])
    }

    func testEveryChunkAndEmbedOptionReachesTheCommandLine() throws {
        let runs = try options {
            $0.chunk.enabled = true
            $0.chunk.skipExisting = true
            $0.chunk.tokenizer = "intfloat/multilingual-e5-large"
            $0.chunk.minTokens = 50
            $0.chunk.maxTokens = 400
            $0.embed.enabled = true
            $0.embed.skipExisting = true
            $0.embed.server = " http://192.168.0.241:1234 "
            $0.embed.model = "text-embedding-e5"
        }.runs(projectRoot: root)
        XCTAssertEqual(runs[1].invocation.arguments, [
            "json_to_chunks.py", "--input-dir", "/json", "--output-dir", "/chunks", "--skip-existing",
            "--tokenizer", "intfloat/multilingual-e5-large", "--min-tokens", "50", "--max-tokens", "400",
        ])
        XCTAssertEqual(runs[2].invocation.arguments, [
            "embed_chunks.py", "--input-dir", "/chunks", "--output-dir", "/vectors", "--skip-existing",
            "--server", "http://192.168.0.241:1234", "--model", "text-embedding-e5",
        ])
    }

    func testProblemsOnlyOfTheStepsThatRun() {
        let empty = options {
            $0.chunk.outputDirectory = ""
            $0.embed.outputDirectory = ""
        }
        XCTAssertEqual(empty.problems(), [])
        XCTAssertEqual(options {
            $0.chunk = ChunkOptions()
            $0.chunk.enabled = true
            $0.chunk.tokenizer = " "
            $0.chunk.minTokens = 480
            $0.embed = EmbedOptions()
            $0.embed.enabled = true
        }.problems(), [
            "Choose the folder the chunks go to.", "Name the tokenizer of the embedding model.",
            "Min tokens must be 0 or more, and smaller than max tokens.", "Choose the folder the vectors go to.",
        ])
        XCTAssertEqual(options {
            $0.chunk.enabled = true
            $0.chunk.minTokens = -1
        }.problems(), ["Min tokens must be 0 or more, and smaller than max tokens."])
    }

    func testStepOptionsSurviveSaving() throws {
        var chunk = ChunkOptions()
        chunk.enabled = true
        chunk.maxTokens = 300
        var embed = EmbedOptions()
        embed.server = "http://127.0.0.1:1234"
        XCTAssertEqual(try JSONDecoder().decode(ChunkOptions.self, from: JSONEncoder().encode(chunk)), chunk)
        XCTAssertEqual(try JSONDecoder().decode(EmbedOptions.self, from: JSONEncoder().encode(embed)), embed)
    }
}

final class PipelineRunnerTests: XCTestCase {
    private static let timeout: TimeInterval = 30
    /// How long a test that failed midway gives its script to stop before it is terminated.
    private static let cleanupTimeout: TimeInterval = 2

    private var fake: FakeProject!

    override func setUpWithError() throws {
        fake = try FakeProject()
    }

    override func tearDownWithError() throws {
        fake?.remove()
    }

    private struct Record {
        var started: [PipelineStep] = []
        var lines: [String] = []
        var outcome: PipelineOutcome?
    }

    /// A fake script: it says which step it is and which folder it reads, prints `status` (run_batch's lines), then
    /// runs `tail` (Python).
    private func script(_ step: PipelineStep, status: [String] = ["== [1/1] a  ->  b", "   ok in 1s"], tail: String = "") -> String {
        let prints = status.map { "print(\($0.debugDescription), flush=True)" }.joined(separator: "\n")
        return """
        import sys
        print("STEP \(step.rawValue) " + sys.argv[sys.argv.index("--input-dir") + 1], flush=True)
        \(prints)
        \(tail)
        """
    }

    /// Writes the scripts (the steps not given get a plain successful one) and runs the whole pipeline.
    private func runPipeline(_ scripts: [PipelineStep: String] = [:], stopWhen: String? = nil) throws -> Record {
        for step in PipelineStep.allCases {
            try fake.write(scripts[step] ?? script(step), for: step)
        }
        var ocr = ScriptOptions()
        ocr.inputDirectory = fake.root.path
        ocr.outputDirectory = fake.root.appendingPathComponent("json").path
        ocr.format = .json
        var chunk = ChunkOptions()
        chunk.enabled = true
        chunk.outputDirectory = fake.root.appendingPathComponent("chunks").path
        var embed = EmbedOptions()
        embed.enabled = true
        embed.outputDirectory = fake.root.appendingPathComponent("vectors").path
        let runs = try PipelineOptions(ocr: ocr, chunk: chunk, embed: embed).runs(projectRoot: fake.root)

        var record = Record()
        let exited = expectation(description: "the pipeline ends")
        var runner: PipelineRunner!
        runner = PipelineRunner(
            runs: runs,
            environment: ChildEnvironment.make(from: ProcessInfo.processInfo.environment),
            onStepStarted: { run, index in
                XCTAssertEqual(index, record.started.count)
                record.started.append(run.step)
            },
            onOutput: { lines, _ in
                record.lines += lines.map(\.text)
                if let stopWhen, lines.contains(where: { $0.text == stopWhen }) {
                    runner.stop()
                }
            },
            onExit: { outcome in
                XCTAssertNil(record.outcome, "two ends")
                record.outcome = outcome
                exited.fulfill()
            }
        )
        addTeardownBlock { runner.stopAndWait(timeout: Self.cleanupTimeout) }
        runner.start()
        wait(for: [exited], timeout: Self.timeout)
        return record
    }

    func testTheStepsRunInOrderEachOnThePreviousOutput() throws {
        let record = try runPipeline()
        XCTAssertEqual(record.outcome, .succeeded)
        XCTAssertEqual(record.started, [.ocr, .chunk, .embed])
        XCTAssertEqual(record.lines.filter { $0.hasPrefix("STEP") }, [
            "STEP ocr \(fake.root.path)",
            "STEP chunk \(fake.root.appendingPathComponent("json").path)",
            "STEP embed \(fake.root.appendingPathComponent("chunks").path)",
        ])
    }

    func testAStepWhereSomeFilesFailedGoesOn() throws {
        let partly = script(.ocr, status: [
            "== [1/2] a.pdf  ->  a.json", "   ok in 1s", "== [2/2] b.pdf  ->  b.json", "   FAILED (bad scan) in 1s",
        ], tail: "sys.exit('failed: b.pdf')")
        let record = try runPipeline([.ocr: partly])
        XCTAssertEqual(record.outcome, .finishedWithFailures([.ocr]))
        XCTAssertEqual(record.started, [.ocr, .chunk, .embed])
    }

    func testAStepThatProducedNothingEndsThePipeline() throws {
        let nothing = """
        import sys
        sys.exit("no .json files in /json")
        """
        let record = try runPipeline([.chunk: nothing])
        XCTAssertEqual(record.outcome, .failed(.chunk, .failed(exitStatus: 1)))
        XCTAssertEqual(record.started, [.ocr, .chunk])
        XCTAssertTrue(record.lines.contains("no .json files in /json"))
    }

    func testStopEndsThePipeline() throws {
        let slow = script(.ocr, status: ["started"], tail: "import time\ntime.sleep(300)")
        let record = try runPipeline([.ocr: slow], stopWhen: "started")
        XCTAssertEqual(record.outcome, .stopped(.ocr))
        XCTAssertEqual(record.started, [.ocr])
    }

    func testAMissingPythonCannotStart() throws {
        try FileManager.default.removeItem(at: fake.root.appendingPathComponent(ProjectLocator.pythonRelativePath))
        let record = try runPipeline()
        guard case .couldNotStart(.ocr, _)? = record.outcome else {
            return XCTFail("\(String(describing: record.outcome))")
        }
        XCTAssertEqual(record.started, [.ocr])
    }
}
