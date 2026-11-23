// The window: the options on the left, the command, the run and its log on the right.

import PdfToMdCore
import SwiftUI

/// pipeline_common's own help for --skip-existing, which every step has.
private let skipExistingHelp = "Skip files whose output exists and is not empty (--skip-existing)."

struct ContentView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HSplitView {
            OptionsForm()
                .frame(minWidth: 380, idealWidth: 440, maxWidth: 600)
            RunPanel()
                .frame(minWidth: 460)
        }
        // PDFs may have been added, or chunk-config.json edited, while the app was in the background.
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.refreshDocuments()
        }
    }
}

private struct OptionsForm: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Section("Project") {
                FolderRow(
                    title: "Project folder", path: model.projectRoot?.path ?? "", placeholder: "Not found",
                    choose: model.chooseProjectFolder
                )
                if let problem = model.projectProblem {
                    Text(problem).foregroundStyle(.red).font(.callout)
                }
            }
            Section("Folders") {
                FolderRow(title: "Files from", path: model.options.inputDirectory, placeholder: "Choose a folder") {
                    model.chooseFolder(\.options.inputDirectory, message: "The folder with the PDFs and Word files", canCreate: false)
                }
                .help("Every .pdf and .docx directly inside this folder is converted: PDFs with OCR (pdf_to_md.py), Word files as they are (docx_to_md.py) (--input-dir).")
                FolderRow(title: "Results to", path: model.options.outputDirectory, placeholder: "Choose a folder") {
                    model.chooseFolder(\.options.outputDirectory, message: "The folder the results go to")
                }
                .help("One .md or .json per file; created if missing (--output-dir).")
                Toggle("Skip files already converted", isOn: $model.options.skipExisting)
                    .help(skipExistingHelp)
            }
            Section("Output") {
                Picker("Format", selection: $model.options.format) {
                    Text("Markdown").tag(OutputFormat.markdown)
                    Text("JSON").tag(OutputFormat.json)
                }
                .pickerStyle(.segmented)
                .help("JSON is Docling's full document, not only its text (--to).")
                Toggle("Save the pictures as PNG files", isOn: $model.options.images)
                    .disabled(!model.options.imagesAllowed)
                    .help("Writes the pictures in <name>_images beside the .json, which refers to them. JSON only (--images).")
                Toggle("Also keep the image of every page", isOn: $model.options.pageImages)
                    .disabled(!model.options.pageImagesAllowed)
                    .help("Large, and not a picture of the document. Needs the pictures option (--page-images).")
            }
            OCRSection()
            Section("Docling") {
                TextField("Extra options", text: $model.options.extraDoclingOptions, prompt: Text("--page-range 1-4"))
                    .font(.system(.body, design: .monospaced))
                    .help("Passed to `docling convert` unchanged, after \"--\". Quote values with spaces.")
            }
            if model.pipeline.chunkingAllowed {
                ChunkingSection()
                EmbeddingSection()
            }
        }
        .formStyle(.grouped)
        .disabled(model.isRunning)
    }
}

/// pdf_to_md.py's OCR options: the PDFs' only; Word files are read as they are.
private struct OCRSection: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Section("OCR") {
            Picker("Engine", selection: $model.options.ocrEngine) {
                ForEach(OCREngines.all, id: \.self) { Text($0).tag($0) }
            }
            .help("For the PDFs; Word files need no OCR. tesseract needs the tesseract command; ocrmac is Apple Vision (--ocr-engine).")
            TextField("Language", text: $model.options.ocrLanguage, prompt: Text("the engine chooses"))
                .help("As the engine names it: ron for Tesseract, ro-RO for Apple Vision (--ocr-lang).")
            Picker("Regions", selection: $model.options.ocrMode) {
                ForEach(OCRMode.allCases) { Text($0.rawValue).tag($0) }
            }
            .help("Which regions are OCR'd; full_page replaces any text layer (--ocr-mode).")
            Picker("Device", selection: $model.options.device) {
                ForEach(Device.allCases) { Text($0.rawValue).tag($0) }
            }
            .help("mps is the Mac's GPU (--device).")
        }
    }
}

/// json_to_chunks.py on the JSON the conversion wrote.
private struct ChunkingSection: View {
    @EnvironmentObject private var model: AppModel
    @State private var editingDocuments = false

    var body: some View {
        Section("Chunking") {
            Toggle("Cut the documents into chunks", isOn: $model.chunkOptions.enabled)
                .help("Runs json_to_chunks.py on the .json files the conversion wrote. Every document needs its entry in chunk-config.json.")
            if model.chunkOptions.enabled {
                LabeledContent("Document settings") {
                    HStack {
                        Text(documentsSummary)
                            .foregroundStyle(model.documentsWithoutSettings.isEmpty && model.documentConfigProblem == nil ? .secondary : Color.orange)
                        Button("Edit…") { editingDocuments = true }
                            .disabled(model.projectRoot == nil || model.options.inputDirectory.isEmpty)
                    }
                }
                .help("Each file's entry in chunk-config.json: its code, title, year and status, and how it is cut.")
                .sheet(isPresented: $editingDocuments, onDismiss: model.refreshDocuments) {
                    if let projectRoot = model.projectRoot {
                        DocumentSettingsView(
                            projectRoot: projectRoot, inputFolder: URL(fileURLWithPath: model.options.inputDirectory)
                        )
                    }
                }
                FolderRow(title: "Chunks to", path: model.chunkOptions.outputDirectory, placeholder: "Choose a folder") {
                    model.chooseFolder(\.chunkOptions.outputDirectory, message: "The folder the chunks go to")
                }
                .help("One .jsonl per document, one chunk per line (--output-dir).")
                Toggle("Skip files already chunked", isOn: $model.chunkOptions.skipExisting)
                    .help(skipExistingHelp)
                TextField("Tokenizer", text: $model.chunkOptions.tokenizer)
                    .help("Hugging Face name of the embedding model's tokenizer; chunk sizes are counted with it (--tokenizer).")
                TextField("Min tokens", text: $model.chunkOptions.minTokens.asText)
                    .help("A smaller article is joined with the next (--min-tokens).")
                TextField("Max tokens", text: $model.chunkOptions.maxTokens.asText)
                    .help("A larger chunk is split; the breadcrumb counts (--max-tokens).")
            }
        }
    }

    private var documentsSummary: String {
        let count = model.documentFiles.count
        let missing = model.documentsWithoutSettings.count
        if model.documentConfigProblem != nil {
            return "\(DocumentConfigFile.fileName) cannot be read"
        }
        if count == 0 {
            return "No PDFs or Word files in the input folder"
        }
        return missing == 0 ? "All \(count) set" : "\(missing) of \(count) not set"
    }
}

/// embed_chunks.py on the chunks the chunking step wrote.
private struct EmbeddingSection: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Section("Embedding") {
            Toggle("Embed the chunks", isOn: $model.embedOptions.enabled)
                .disabled(!model.pipeline.embeddingAllowed)
                .help("Runs embed_chunks.py on the chunks of this run. Needs chunking, and a server with the model loaded.")
            if model.embedOptions.enabled {
                FolderRow(title: "Vectors to", path: model.embedOptions.outputDirectory, placeholder: "Choose a folder") {
                    model.chooseFolder(\.embedOptions.outputDirectory, message: "The folder the vectors go to")
                }
                .help("One .npz of vectors per .jsonl (--output-dir).")
                Toggle("Skip files already embedded", isOn: $model.embedOptions.skipExisting)
                    .help(skipExistingHelp)
                TextField("Server", text: $model.embedOptions.server, prompt: Text("from settings.env"))
                    .help("Address of the server that runs the model, e.g. http://127.0.0.1:1234. Empty: NORMS_EMBEDDING_SERVER in settings.env, else LM Studio on this Mac (--server).")
                TextField("Model", text: $model.embedOptions.model, prompt: Text("the script's default (bge-m3)"))
                    .help("The embedding model's id on the server. Empty: the script's default (--model).")
            }
        }
    }
}

private struct FolderRow: View {
    let title: String
    let path: String
    let placeholder: String
    let choose: () -> Void

    var body: some View {
        LabeledContent(title) {
            HStack {
                Text(path.isEmpty ? placeholder : (path as NSString).abbreviatingWithTildeInPath)
                    .foregroundStyle(path.isEmpty ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(path)
                Button("Choose…", action: choose)
            }
        }
    }
}

private struct RunPanel: View {
    @EnvironmentObject private var model: AppModel
    private static let monospaced = Font.system(.callout, design: .monospaced)

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            command
            ForEach(model.problems, id: \.self) { problem in
                Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
            HStack {
                Button(model.pipeline.runsSeveralSteps ? "Run" : "Convert", action: model.start)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.canStart)
                Button("Stop", action: model.stop)
                    .disabled(model.state != .running)
                Spacer()
                Button("Show Results", action: model.showOutputFolder)
                    .disabled(model.options.outputDirectory.isEmpty)
            }
            status
            LogView(lines: model.log, font: Self.monospaced)
            HStack {
                Spacer()
                Button("Copy Log", action: model.copyLog)
                    .disabled(model.log.isEmpty)
            }
        }
        .padding()
    }

    @ViewBuilder private var command: some View {
        GroupBox {
            HStack(alignment: .top) {
                switch model.runs {
                case .success(let runs)?:
                    Text(runs.map(\.invocation.commandLine).joined(separator: "\n"))
                        .font(Self.monospaced)
                        .textSelection(.enabled)
                case .failure(let error)?:
                    Text(error.localizedDescription).foregroundStyle(.orange)
                case nil:
                    Text("No project folder.").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Copy", action: model.copyCommands)
            }
        } label: {
            Text("Commands, one per step (in the project folder)")
        }
    }

    @ViewBuilder private var status: some View {
        let progress = model.progress
        if let position = model.position, position.count > 1 {
            Text("Step \(position.number) of \(position.count): \(position.step.title)").font(.headline)
        }
        if let count = progress.fileCount {
            ProgressView(value: Double(progress.finishedCount), total: Double(max(count, 1))) {
                Text(model.isRunning ? "File \(progress.fileNumber) of \(count): \(progress.currentFile ?? "")" : "\(count) file\(count == 1 ? "" : "s")")
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        HStack {
            Text(stateText).foregroundStyle(stateColor)
            if progress.fileCount != nil {
                Text("· \(progress.converted) converted, \(progress.skipped) skipped, \(progress.failedFiles.count) failed")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var stateText: String {
        switch model.state {
        case .idle:
            return "Ready."
        case .running:
            return "\(model.position?.step.title ?? "Starting")…"
        case .stopping:
            return "Stopping…"
        case .finished(.succeeded):
            return "Done."
        case .finished(.finishedWithFailures(let steps)):
            return "Done, but some files failed in \(steps.map(\.title).joined(separator: ", ")). See the log."
        case .finished(.stopped(let step)):
            return "Stopped during \(step.title)."
        case .finished(.failed(let step, let outcome)):
            let later = model.position.map { $0.number < $0.count } ?? false
            return "\(step.title) failed (\(Self.describe(outcome)))" + (later ? "; the steps after it did not run." : ".")
        case .finished(.couldNotStart(let step, let reason)):
            return "Could not start \(step.title): \(reason)"
        }
    }

    private static func describe(_ outcome: RunOutcome) -> String {
        switch outcome {
        case .failed(let status): return "exit status \(status)"
        case .killed(let signal): return "ended by signal \(signal)"
        case .succeeded, .stopped: return "\(outcome)"
        }
    }

    private var stateColor: Color {
        switch model.state {
        case .finished(.succeeded): return .green
        case .finished(.finishedWithFailures): return .orange
        case .finished(.failed), .finished(.couldNotStart): return .red
        default: return .primary
        }
    }
}

private struct LogView: View {
    let lines: [AppModel.LogLine]
    let font: Font

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(lines) { line in
                        Text(line.text.isEmpty ? " " : line.text)
                            .font(font)
                            .foregroundStyle(color(of: line.kind))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.id)
                    }
                }
                .textSelection(.enabled)
                .padding(8)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .border(Color(nsColor: .separatorColor))
            .onChange(of: lines.last?.id) { _, id in
                if let id {
                    proxy.scrollTo(id, anchor: .bottom)
                }
            }
        }
    }

    private func color(of kind: RunProgress.LineKind) -> Color {
        switch kind {
        case .fileStarted: return .accentColor
        case .converted: return .green
        case .failed: return .red
        case .skipped, .other: return .primary
        }
    }
}
