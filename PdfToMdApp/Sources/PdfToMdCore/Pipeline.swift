// The steps after OCR, which read its JSON: json_to_chunks.py cuts the documents into chunks, embed_chunks.py turns the
// chunks into vectors. Their options and defaults are the scripts' own: change them there first, then here.

import Foundation

public enum PipelineStep: String, CaseIterable, Sendable {
    /// pdf_to_md.py: a PDF, OCR'd.
    case ocr
    /// docx_to_md.py: a Word file, read as it is (no OCR).
    case word
    case chunk, embed

    public var scriptName: String {
        switch self {
        case .ocr: return "pdf_to_md.py"
        case .word: return "docx_to_md.py"
        case .chunk: return "json_to_chunks.py"
        case .embed: return "embed_chunks.py"
        }
    }

    public var title: String {
        switch self {
        case .ocr: return "OCR"
        case .word: return "Word conversion"
        case .chunk: return "Chunking"
        case .embed: return "Embedding"
        }
    }
}

/// The kinds of file the input folder can hold; each has its own conversion script (pipeline_common.collect_files
/// takes one suffix), and a folder with both gets both, into the same results folder.
public enum SourceKind: String, CaseIterable, Identifiable, Codable, Sendable {
    case pdf, docx
    public var id: String { rawValue }

    /// The suffix of the files, without its dot.
    public var fileExtension: String { rawValue }

    public var conversionStep: PipelineStep {
        switch self {
        case .pdf: return .ocr
        case .docx: return .word
        }
    }

    /// Only a PDF has OCR options; docx_to_md.py takes none.
    public var hasOCR: Bool { self == .pdf }
}

extension String {
    /// A typed value without the spaces around it, which a pasted name or address often carries.
    var trimmed: String { trimmingCharacters(in: .whitespaces) }
}

/// The flags of json_to_chunks.py and embed_chunks.py beyond the folder ones.
extension Flag {
    static let tokenizer = "--tokenizer"
    static let minTokens = "--min-tokens"
    static let maxTokens = "--max-tokens"
    static let server = "--server"
    static let model = "--model"
    /// The Word files the JSON came from: json_to_chunks.py puts back the paragraph numbers Docling drops.
    static let docxDirectory = "--docx-dir"
}

public struct ChunkOptions: Equatable, Codable, Sendable {
    /// json_to_chunks.py's defaults.
    public static let defaultTokenizer = "BAAI/bge-m3"
    public static let defaultMinTokens = 80
    public static let defaultMaxTokens = 480

    public var enabled = false
    public var outputDirectory = ""
    public var skipExisting = false
    /// The embedding model's tokenizer, on Hugging Face: the chunks are sized with it.
    public var tokenizer = defaultTokenizer
    public var minTokens = defaultMinTokens
    public var maxTokens = defaultMaxTokens

    public init() {}

    func arguments(inputDirectory: String, docxDirectory: String?) -> [String] {
        Flag.directories(input: inputDirectory, output: outputDirectory, skipExisting: skipExisting)
            + (docxDirectory.map { [Flag.docxDirectory, $0] } ?? [])
            + [Flag.tokenizer, tokenizer.trimmed, Flag.minTokens, String(minTokens), Flag.maxTokens, String(maxTokens)]
    }

    func problems() -> [String] {
        var found: [String] = []
        if outputDirectory.isEmpty {
            found.append("Choose the folder the chunks go to.")
        }
        if tokenizer.trimmed.isEmpty {
            found.append("Name the tokenizer of the embedding model.")
        }
        if !(0 <= minTokens && minTokens < maxTokens) {  // the script's own rule
            found.append("Min tokens must be 0 or more, and smaller than max tokens.")
        }
        return found
    }
}

public struct EmbedOptions: Equatable, Codable, Sendable {
    public var enabled = false
    public var outputDirectory = ""
    public var skipExisting = false
    /// Empty: the script's default, NORMS_EMBEDDING_SERVER in settings.env, else LM Studio on this Mac.
    public var server = ""
    /// The model's id on the server. Empty: the script's default (bge-m3, the model the chunks are sized for).
    public var model = ""

    public init() {}

    func arguments(inputDirectory: String) -> [String] {
        var arguments = Flag.directories(input: inputDirectory, output: outputDirectory, skipExisting: skipExisting)
        for (flag, value) in [(Flag.server, server.trimmed), (Flag.model, model.trimmed)] where !value.isEmpty {
            arguments += [flag, value]
        }
        return arguments
    }

    func problems() -> [String] {
        outputDirectory.isEmpty ? ["Choose the folder the vectors go to."] : []
    }
}

public struct PipelineRun: Equatable, Sendable {
    public let step: PipelineStep
    public let invocation: Invocation
}

/// The conversion of each kind of file the input folder holds (OCR for PDFs, Docling's Word reader for .docx, both
/// into the results folder); then, when the output is JSON and chunking is on, chunking; then, when chunking runs and
/// embedding is on, embedding. Each step reads the folder the step before wrote.
public struct PipelineOptions: Equatable, Sendable {
    /// The kinds of file in the input folder.
    public var sources: Set<SourceKind>
    /// The conversions' options; the OCR ones go to the PDFs only.
    public var ocr: ScriptOptions
    public var chunk: ChunkOptions
    public var embed: EmbedOptions

    public init(sources: Set<SourceKind> = [.pdf], ocr: ScriptOptions, chunk: ChunkOptions, embed: EmbedOptions) {
        self.sources = sources
        self.ocr = ocr
        self.chunk = chunk
        self.embed = embed
    }

    /// The conversions to run, PDFs first. With no files known yet (no folder chosen), the PDF one, so that there is
    /// still a command to show.
    var conversions: [SourceKind] {
        sources.isEmpty ? [.pdf] : SourceKind.allCases.filter { sources.contains($0) }
    }

    /// json_to_chunks.py reads Docling's JSON, not the Markdown.
    public var chunkingAllowed: Bool { ocr.format == .json }
    public var runsChunking: Bool { chunkingAllowed && chunk.enabled }
    /// embed_chunks.py reads the chunks the same run writes.
    public var embeddingAllowed: Bool { runsChunking }
    var runsEmbedding: Bool { embeddingAllowed && embed.enabled }
    /// More than OCR alone.
    public var runsSeveralSteps: Bool { runsChunking }

    public func runs(projectRoot: URL) throws -> [PipelineRun] {
        var runs = try conversions.map {
            PipelineRun(step: $0.conversionStep, invocation: try Invocation(projectRoot: projectRoot, options: ocr, source: $0))
        }
        if runsChunking {
            // Only with Word files there: json_to_chunks.py stops on a --docx-dir that has none, and a JSON from a PDF
            // simply has no Word file to take labels from.
            let docxDirectory = sources.contains(.docx) ? ocr.inputDirectory : nil
            runs.append(run(.chunk, projectRoot, chunk.arguments(inputDirectory: ocr.outputDirectory, docxDirectory: docxDirectory)))
        }
        if runsEmbedding {
            runs.append(run(.embed, projectRoot, embed.arguments(inputDirectory: chunk.outputDirectory)))
        }
        return runs
    }

    /// What stops a run, in plain words; empty when the options can be run.
    public func problems() -> [String] {
        ocr.problems(refusingOCROptions: conversions.contains(.pdf))
            + (runsChunking ? chunk.problems() : []) + (runsEmbedding ? embed.problems() : [])
    }

    private func run(_ step: PipelineStep, _ projectRoot: URL, _ arguments: [String]) -> PipelineRun {
        PipelineRun(step: step, invocation: Invocation(projectRoot: projectRoot, script: step.scriptName, arguments: arguments))
    }
}
