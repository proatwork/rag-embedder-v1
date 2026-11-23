// The Document settings sheet: the files of the input folder on the left, the selected one's chunk-config.json entry on
// the right. Nothing is written until Save, and then the whole file at once.

import PdfToMdCore
import SwiftUI

@MainActor
final class DocumentSettingsModel: ObservableObject {
    let files: [DocumentFile]
    /// Why the file cannot be edited: the app does not change a file it cannot read.
    let loadError: String?
    @Published var entries: [String: DocumentEntry]
    @Published var selection: DocumentFile.ID?
    private let configURL: URL
    private let original: [String: DocumentEntry]

    init(projectRoot: URL, inputFolder: URL) {
        configURL = DocumentConfigFile.url(in: projectRoot)
        files = DocumentFiles.files(in: inputFolder)
        do {
            original = try DocumentConfigFile.load(from: configURL)
            loadError = nil
        } catch {
            original = [:]
            loadError = "\(error.localizedDescription) Fix it by hand first."
        }
        entries = original
        selection = (files.first { original[$0.key] == nil } ?? files.first)?.id  // a file that still needs settings
    }

    var selectedFile: DocumentFile? {
        files.first { $0.id == selection }
    }

    func problems(of file: DocumentFile) -> [String] {
        entries[file.key]?.normalized().problems() ?? []
    }

    /// The problems of entries whose file is not in the folder: the sheet cannot show them, and the chunker refuses
    /// the whole file while any is there.
    var otherProblems: [String] {
        DocumentConfigFile.problems(in: entries, excluding: Set(files.map(\.key)))
    }

    /// Only the listed files' entries hold Save back: an entry this sheet cannot show must not lock it.
    var canSave: Bool {
        loadError == nil && entries != original && files.allSatisfy { problems(of: $0).isEmpty }
    }

    func addSuggestedEntry(for file: DocumentFile) {
        entries[file.key] = .suggested(forKey: file.key, currentYear: Calendar.current.component(.year, from: Date()))
    }

    func save() throws {
        try DocumentConfigFile.save(entries.mapValues { $0.normalized() }, to: configURL)
    }
}

struct DocumentSettingsView: View {
    @StateObject private var settings: DocumentSettingsModel
    @Environment(\.dismiss) private var dismiss
    @State private var saveError: String?

    init(projectRoot: URL, inputFolder: URL) {
        _settings = StateObject(wrappedValue: DocumentSettingsModel(projectRoot: projectRoot, inputFolder: inputFolder))
    }

    var body: some View {
        VStack(spacing: 0) {
            ForEach(banners, id: \.self) { message in
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding([.horizontal, .top])
            }
            HSplitView {
                List(settings.files, selection: $settings.selection) { file in
                    FileRow(file: file, state: state(of: file))
                }
                .frame(minWidth: 200, idealWidth: 240, maxWidth: 360)
                detail
                    .frame(minWidth: 400, maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            HStack {
                Text("Saved to \(DocumentConfigFile.fileName) in the project folder.").foregroundStyle(.secondary)
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Save", action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!settings.canSave)
            }
            .padding()
        }
        .frame(minWidth: 760, minHeight: 520)
    }

    private var banners: [String] {
        if let loadError = settings.loadError {
            return [loadError]
        }
        let others = settings.otherProblems
        return (saveError.map { [$0] } ?? [])
            + (others.isEmpty ? [] : ["Fix by hand before chunking (their files are not in this folder): " + others.joined(separator: "; ")])
    }

    @ViewBuilder private var detail: some View {
        if settings.files.isEmpty {
            Text("No PDFs or Word files in the input folder.").foregroundStyle(.secondary)
        } else if settings.loadError != nil {
            Text("Nothing can be shown or changed until \(DocumentConfigFile.fileName) can be read.").foregroundStyle(.secondary)
        } else if let file = settings.selectedFile {
            if settings.entries[file.key] != nil {
                VStack(alignment: .leading, spacing: 0) {
                    EntryForm(entry: entryBinding(for: file.key))
                    ForEach(settings.problems(of: file), id: \.self) { problem in
                        Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                            .padding(.horizontal)
                    }
                    .padding(.bottom, 4)
                }
            } else {
                VStack(spacing: 12) {
                    Text("\(file.name) has no settings yet, so chunking would fail on it.")
                    Button("Add Settings") { settings.addSuggestedEntry(for: file) }
                }
            }
        } else {
            Text("Select a file.").foregroundStyle(.secondary)
        }
    }

    private func state(of file: DocumentFile) -> FileRow.State {
        guard settings.loadError == nil else {
            return .unknown
        }
        guard settings.entries[file.key] != nil else {
            return .missing
        }
        return settings.problems(of: file).isEmpty ? .ready : .invalid
    }

    private func entryBinding(for key: String) -> Binding<DocumentEntry> {
        Binding(
            get: { settings.entries[key] ?? .suggested(forKey: key, currentYear: Calendar.current.component(.year, from: Date())) },
            set: { settings.entries[key] = $0 }
        )
    }

    private func save() {
        saveError = nil
        do {
            try settings.save()
            dismiss()
        } catch {
            saveError = "Could not save: \(error.localizedDescription)"
        }
    }
}

private struct FileRow: View {
    enum State {
        case ready, missing, invalid, unknown
    }

    let file: DocumentFile
    let state: State

    var body: some View {
        switch state {
        case .unknown:
            Label(file.name, systemImage: "questionmark.circle").foregroundStyle(.secondary)
                .help("Not known: \(DocumentConfigFile.fileName) cannot be read.")
        case .ready:
            Label(file.name, systemImage: "checkmark.circle").help("Has its settings.")
        case .missing:
            Label(file.name, systemImage: "circle.dashed").foregroundStyle(.orange).help("No settings yet.")
        case .invalid:
            Label(file.name, systemImage: "exclamationmark.triangle").foregroundStyle(.red).help("Its settings have a problem.")
        }
    }
}

/// A number field as text, taken on every keystroke: a formatted TextField takes its value only on Return or when it
/// loses focus, so a click on Save or Run right after typing would miss it. Text that is no number leaves the value.
extension Binding where Value == Int {
    var asText: Binding<String> {
        Binding<String>(get: { String(wrappedValue) }, set: { text in Int(text).map { wrappedValue = $0 } })
    }
}

extension Binding where Value == Int? {
    /// An empty field is nil.
    var asText: Binding<String> {
        Binding<String>(
            get: { wrappedValue.map(String.init) ?? "" },
            set: { text in
                if text.isEmpty {
                    wrappedValue = nil
                } else if let number = Int(text) {
                    wrappedValue = number
                }
            }
        )
    }
}

/// One document's entry. The help texts say which key of chunk-config.json each field is.
private struct EntryForm: View {
    @Binding var entry: DocumentEntry

    var body: some View {
        Form {
            Section("What every chunk says of it") {
                TextField("Code", text: $entry.code, prompt: Text("e.g. P118/2-2013"))
                    .help("The short name the answers cite (code).")
                TextField("Title", text: $entry.title)
                    .help("The norm's full title (title).")
                TextField("Year", text: $entry.year.asText)
                    .help("The year of this edition (year).")
                Picker("Status", selection: $entry.status) {
                    Text("In force").tag(DocumentStatus.inForce)
                    Text("Unverified").tag(DocumentStatus.unverified)
                    Text("Repealed").tag(DocumentStatus.repealed)
                }
                .help("Only “In force” says the norm applies; “Unverified” is for one nobody has checked (status).")
                if entry.status == .repealed {
                    TextField("Repealed on", text: optionalText($entry.repealedOn), prompt: Text("YYYY-MM-DD"))
                        .help("The day it stopped applying (repealed_on).")
                }
                TextField("Gazette", text: optionalText($entry.gazette), prompt: Text("e.g. M. Of. nr. 595 bis/2013"))
                    .help("Where it was published (gazette). Optional.")
                TextField("Amended by", text: amendedBy, prompt: Text("one act per line"), axis: .vertical)
                    .lineLimit(2...6)
                    .help("The acts that changed it after this text, one per line (amended_by). Optional.")
            }
            Section("How it is cut into chunks") {
                Picker("Numbering", selection: $entry.profile) {
                    Text("Articles (Art. 12)").tag(ChunkProfile.art)
                    Text("Numbered paragraphs (3.7.4.2)").tag(ChunkProfile.decimal)
                    Text("None: text under each heading").tag(ChunkProfile.headings)
                }
                .help("Where a chunk starts (profile).")
                TextField("Text starts on page", text: $entry.firstBodyPage.asText, prompt: Text("found by itself"))
                    .help("The PDF page the norm's text starts on, after the cover and contents (first_body_page). Optional.")
            }
            Section("Embedding") {
                Picker("Embedded text", selection: embedField) {
                    Text("Headings path and text").tag(EmbedField.embedText)
                    Text("Text only").tag(EmbedField.text)
                }
                .help("What of each chunk gets its vector (embed_field). The first is the script's default.")
            }
        }
        .formStyle(.grouped)
    }

    /// An empty field leaves the key out.
    private func optionalText(_ value: Binding<String?>) -> Binding<String> {
        Binding(get: { value.wrappedValue ?? "" }, set: { value.wrappedValue = $0.isEmpty ? nil : $0 })
    }

    /// One act per line; the empty lines go when it is saved, so typing a new line does not lose it.
    private var amendedBy: Binding<String> {
        Binding(
            get: { entry.amendedBy?.joined(separator: "\n") ?? "" },
            set: { entry.amendedBy = $0.isEmpty ? nil : $0.components(separatedBy: "\n") }
        )
    }

    /// The default is written only when it was there already or another value is picked.
    private var embedField: Binding<EmbedField> {
        Binding(
            get: { entry.embedField ?? .embedText },
            set: { entry.embedField = ($0 == .embedText && entry.embedField == nil) ? nil : $0 }
        )
    }
}
