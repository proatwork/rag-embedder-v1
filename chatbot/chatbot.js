"use strict";
const $ = (id) => document.getElementById(id);
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* the page works without it */ } },
  remove(key) { try { localStorage.removeItem(key); } catch (e) { /* the page works without it */ } },
  works() { try { localStorage.setItem("probe", "1"); localStorage.removeItem("probe"); return true; } catch (e) { return false; } },
};
const FIELDS = ["embedding_server", "chat_server", "chat_api", "chat_path", "chat_api_key", "top", "doc", "model", "search_mode"];
const USUAL_PATH = { lmstudio: "/api/v1/chat", openai: "/chat/completions" };
const SEARCH_BY_MODEL = "model";  // the "search" of a question: the model searches for itself, more than once if it needs to
const SEARCH_ONCE = "once";  // one search with the question's own words, before the model is asked
const SEARCH_MODES = [SEARCH_BY_MODEL, SEARCH_ONCE];  // the options of #search_mode
const SEARCH_MODE_NOTE = "With the LM Studio style the norms are searched once, with the question's own words: the model can search for itself only with the OpenAI style.";
const STATUS_SEARCHING = "Searching the norms…";
const STATUS_WAITING = "Waiting for the model…";
const STATUS_READING_SEARCH = "Reading the results of search {number}…";
const SEARCH_NUMBER_END = ". ";  // after a search's own number, at the start of its line
const SEARCH_QUOTE = '"';  // around the words the model searched for, shown as it wrote them
const SEARCH_ARTICLE = "number ";  // before the number of an article the model fetched as it is
const SEARCH_NOT_RUN = "not run: ";
const SEARCH_RESULT = " result";
const SEARCH_RESULTS = " results";
const SEARCH_NEW = " ({count} new)";  // of the results, those no earlier search of the answer gave
const SOURCE_BY_NUMBER = "by number";  // in place of the score of a source that was fetched, not searched
const SOURCE_SCORE = "score ";
const SOURCE_SEARCH = "search ";  // before the number of the search that first gave a source
const THINKING_BREAK = "\n\n———\n\n";  // in the thinking text, between what the model thought before a search and after it
const SCROLL_SLACK = 80;
const MAX_INPUT_PX = 160;
const ROW_SEPARATOR = " · ";  // between the parts of a source's row
const NARROW_SCREEN = "(max-width: 700px)";  // as in chatbot.css: there the sidebar lies over the chat
const EMPTY_TITLE = "Ask a question about the norms";
const EMPTY_HINT = 'The answer is written only from the chunks found for it: open "Sources" under an answer to check it.';
const EXAMPLES_LANGUAGE = "ro";
const EMPTY_EXAMPLES = [  // a click puts one in the question box
  "Ce rezistență la foc trebuie să aibă un perete antifoc?",
  "Cum se amplasează detectoarele de fum într-o încăpere?",
  "Câte lifturi de pompieri sunt obligatorii într-o clădire foarte înaltă?",
];
const THINK_BLOCK = /^\s*<think>([\s\S]*?)(<\/think>|$)/;
const HISTORY_KEY = "chat_history";
const MAX_HISTORY = 100;
const HISTORY_EXCHANGES_SENT = 6;  // the newest questions with their answers that go to the model with a question (ask.MAX_HISTORY_MESSAGES / 2)
let controller = null;
let maxTop = 50;
let modelRequest = 0;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---- chat history --------------------------------------------------------------------------------------------------
function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { return []; }
}

function saveHistory(list) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY))); } catch { /* quota full */ }
}

let currentConversation = { id: null, title: "", created: "", messages: [] };

// The earlier exchange sent to the model with the next question: the newest answered pairs, as user / assistant messages.
// Failed or empty answers are left out, with their questions.
function historyForModel() {
  const pairs = [];
  const messages = currentConversation.messages;
  for (let i = 0; i + 1 < messages.length; i += 2) {
    const question = messages[i], reply = messages[i + 1];
    if (question.role !== "user" || reply.role !== "bot" || !reply.content || !reply.content.trim() || (reply.errors && reply.errors.length)) continue;
    pairs.push([{ role: "user", content: question.content }, { role: "assistant", content: reply.content.trim() }]);
  }
  return pairs.slice(-HISTORY_EXCHANGES_SENT).flat();
}

function saveToHistory(conversation) {
  if (!conversation.messages.length) return;
  const conv = {
    id: conversation.id || Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    title: conversation.title, created: conversation.created || new Date().toISOString(),
    updated: new Date().toISOString(), messages: conversation.messages,
  };
  const history = loadHistory();
  const idx = history.findIndex(h => h.id === conv.id);
  if (idx >= 0) history[idx] = conv; else history.unshift(conv);
  saveHistory(history);
  conversation.id = conv.id;
}

function saveCurrentToHistory() { saveToHistory(currentConversation); }

function closeSidebarOnNarrowScreen() {
  if (window.matchMedia(NARROW_SCREEN).matches) $("sidebar").classList.add("collapsed");
}

function newConversation() {
  closeSidebarOnNarrowScreen();
  saveCurrentToHistory();
  currentConversation = { id: null, title: "", created: "", messages: [] };
  showEmptyState();
  renderHistory();
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  if (now - d < 86400000 && now.getDate() === d.getDate()) return pad(d.getHours()) + ":" + pad(d.getMinutes());
  return date;
}

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  for (const conv of loadHistory()) {
    const item = el("div", "history-item");
    if (conv.id === currentConversation.id) item.classList.add("active");
    const title = el("div", "title", conv.title || "Untitled");
    const msgCount = conv.messages ? Math.ceil(conv.messages.length / 2) : 0;
    const meta = el("div", "meta", msgCount + (msgCount === 1 ? " exchange" : " exchanges") + " · " + fmtDate(conv.updated || conv.created));
    const del = el("button", "delete", "✕");
    del.title = "Delete this conversation";
    del.addEventListener("click", (event) => { event.stopPropagation(); deleteConversation(conv.id); });
    item.append(title, meta, del);
    item.addEventListener("click", () => loadConversation(conv.id));
    list.append(item);
  }
}

function loadConversation(id) {
  closeSidebarOnNarrowScreen();
  saveCurrentToHistory();
  const history = loadHistory();
  const conv = history.find(h => h.id === id);
  if (!conv) { newConversation(); return; }
  currentConversation = { id: conv.id, title: conv.title, created: conv.created, messages: conv.messages.slice() };
  const box = $("chat");
  box.replaceChildren();
  for (const msg of currentConversation.messages) {
    if (msg.role === "user") {
      box.append(el("div", "msg user", msg.content));
    } else {
      renderSavedBotMessage(box, msg);
    }
  }
  box.scrollTop = box.scrollHeight;
  renderHistory();
}

function deleteConversation(id) {
  let history = loadHistory();
  history = history.filter(h => h.id !== id);
  saveHistory(history);
  if (currentConversation.id === id) newConversation(); else renderHistory();
}

// ---- settings and the model list -----------------------------------------------------------------------------------
async function loadSettings() {
  const defaults = await (await fetch("/api/settings")).json();
  maxTop = defaults.max_top;
  $("embedding_server").value = store.get("embedding_server", defaults.embedding_server);
  $("chat_server").value = store.get("chat_server", defaults.chat_server);
  $("chat_api").value = store.get("chat_api", "lmstudio");
  $("chat_path").value = store.get("chat_path", "");
  $("chat_api_key").value = store.get("chat_api_key", "");
  $("model").value = store.get("model", "");
  $("search_mode").value = SEARCH_MODES.includes(store.get("search_mode", "")) ? store.get("search_mode", "") : SEARCH_BY_MODEL;
  showUsualPath();
  $("top").value = store.get("top", defaults.top);
  $("top").max = maxTop;
  const doc = $("doc");
  doc.append(new Option("All norms", ""));
  for (const code of defaults.docs) doc.append(new Option(code, code));
  doc.value = defaults.docs.includes(store.get("doc", "")) ? store.get("doc", "") : "";
  await loadModels();
}

const USUAL_LIST = { lmstudio: "/api/v0/models", openai: "/models" };

function showUsualPath() {
  const api = $("chat_api").value, base = $("chat_server").value.trim().replace(/\/+$/, "");
  $("chat_path").placeholder = USUAL_PATH[api] || "";
  $("chat_api_key").disabled = api !== "openai";
  $("showkey").disabled = $("forgetkey").disabled = api !== "openai";
  $("search_mode").disabled = api !== "openai";
  $("searchNote").textContent = api === "openai" ? "" : SEARCH_MODE_NOTE;
  $("endpoint").textContent = "Answers are asked from " + base + ($("chat_path").value.trim() || USUAL_PATH[api]) + " · the model list from " + base + USUAL_LIST[api];
}

async function loadModels() {
  const list = $("models"), input = $("model"), note = $("modelsNote");
  const mine = ++modelRequest;
  note.className = "note";
  note.textContent = "Loading the model list…";
  list.replaceChildren();
  try {
    const key = $("chat_api").value === "openai" ? $("chat_api_key").value.trim() : "";
    const reply = await fetch(
      "/api/models?server=" + encodeURIComponent($("chat_server").value.trim()) + "&api=" + encodeURIComponent($("chat_api").value),
      key ? { headers: { "X-Chat-Api-Key": key } } : {},
    );
    const models = await reply.json();
    if (mine !== modelRequest) return;
    if (!reply.ok) throw new Error(models.error || reply.status);
    for (const m of models) list.append(new Option(m.id, m.id));
    if (models.length && !input.value.trim()) {
      const saved = store.get("model", "");
      input.value = models.some((m) => m.id === saved) ? saved : models[0].id;
    }
    const states = models.filter((m) => m.state).map((m) => m.id + " (" + (m.state === "loaded" ? "loaded" + (m.context ? ", context " + m.context : "") : "not loaded") + ")");
    note.textContent = models.length ? (states.length ? "Models: " + states.join(" · ") : "") : "That server lists no chat models: type the model's id.";
    if (!models.length) note.className = "note bad";
  } catch (error) {
    if (mine !== modelRequest) return;
    note.className = "note bad";
    note.textContent = "Could not get the model list (you can still type the model's id): " + error.message;
  }
}

function saveSettings() {
  for (const id of FIELDS) if (id !== "model" || $(id).value.trim()) store.set(id, $(id).value);
  if (!$("chat_api_key").value) store.remove("chat_api_key");
}

// ---- the conversation ----------------------------------------------------------------------------------------------
function nearBottom(box) { return box.scrollHeight - box.scrollTop - box.clientHeight < SCROLL_SLACK; }

// ---- markdown --------------------------------------------------------------------------------------------------------
// An answer is Markdown. It is turned into elements one by one (never into HTML text), so nothing the model writes can run
// as a script. It is drawn again at every piece of a stream: what is not closed yet (a lone "**", half a table) stays text.
const MD_TAB = "    ";
const MD_HEADING_SHIFT = 2;  // "#" is an h3: the page's own title is the h1
const MD_MAX_HEADING = 6;
const MD_LIST_START = 1;  // the number a numbered list usually begins with
const CELL_MARK = "|";  // between the cells of a table's row, in Markdown and in a source's text
const MIN_TABLE_ROWS = 2;  // of a source's text: one line with a "|" in it is not a table
const MD_FENCE = /^(\s*)(`{3,}|~{3,})/;
const MD_HEADING = /^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const MD_RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const MD_QUOTE = /^ {0,3}>\s?(.*)$/;
const MD_ITEM = /^(\s*)([-*+]|(\d{1,9})[.)])\s+(.*)$/;
const MD_TABLE_RULE = /^\s*\|?(?:\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?\s*$/;
const MD_CELL_BREAK = /(?<!\\)\|/;
const MD_LINK_REL = "noopener noreferrer";
const MD_INLINE = new RegExp([  // the first that matches at a place wins
  /\\(?<escaped>[\\`*_{}[\]()#+.!|~>-])/u,
  /(?<!`)(?<ticks>`+)(?!`)(?<code>.+?)(?<!`)\k<ticks>(?!`)/u,
  /\*\*(?=\S)(?<bold>.+?)(?<=\S)\*\*(?!\*)/u,
  /(?<![\p{L}\p{N}_])__(?=\S)(?<boldAlt>.+?)(?<=\S)__(?![\p{L}\p{N}_])/u,
  /\*(?=[^\s*])(?<em>.+?)(?<=[^\s*])\*(?!\*)/u,
  /(?<![\p{L}\p{N}_])_(?=[^\s_])(?<emAlt>.+?)(?<=[^\s_])_(?![\p{L}\p{N}_])/u,
  /~~(?=\S)(?<del>.+?)(?<=\S)~~/u,
  /\[(?<label>[^[\]]+)\]\((?<href>https?:\/\/[^\s()]+)\)/u,
  /(?<br><br\s*\/?>)/u,  // models break a line inside a table cell with it
  /\[(?<cite>\d+)\]/u,
].map((part) => part.source).join("|"), "giu");

function mdIndent(line) { return line.length - line.trimStart().length; }

function mdCells(line) {
  return line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(MD_CELL_BREAK).map((cell) => cell.trim());
}

function mdIsTable(lines, i) {
  return lines[i].includes(CELL_MARK) && i + 1 < lines.length && MD_TABLE_RULE.test(lines[i + 1]) && mdCells(lines[i]).length === mdCells(lines[i + 1]).length;
}

function mdStartsBlock(lines, i) {
  return [MD_FENCE, MD_HEADING, MD_RULE, MD_QUOTE, MD_ITEM].some((kind) => kind.test(lines[i])) || mdIsTable(lines, i);
}

// Inside a paragraph, a line that begins with a number other than 1 ("2013. A fost…") is its text, not a list.
function mdEndsParagraph(lines, i) {
  const item = lines[i].match(MD_ITEM);
  return mdStartsBlock(lines, i) && !(item && item[3] !== undefined && Number(item[3]) !== MD_LIST_START);
}

// cite(number, label) gives the link of a citation, or null for a number that is no source
function mdInline(target, text, cite) {
  let last = 0;
  for (const match of text.matchAll(MD_INLINE)) {
    const found = match.groups;
    const wrap = (tag, inner) => { const node = el(tag); mdInline(node, inner, cite); return node; };
    let node;
    if (found.escaped !== undefined) node = found.escaped;
    else if (found.code !== undefined) node = el("code", "", found.code);
    else if (found.bold !== undefined) node = wrap("strong", found.bold);
    else if (found.boldAlt !== undefined) node = wrap("strong", found.boldAlt);
    else if (found.em !== undefined) node = wrap("em", found.em);
    else if (found.emAlt !== undefined) node = wrap("em", found.emAlt);
    else if (found.del !== undefined) node = wrap("del", found.del);
    else if (found.br !== undefined) node = el("br");
    else if (found.href !== undefined) {
      node = wrap("a", found.label);
      node.href = found.href; node.target = "_blank"; node.rel = MD_LINK_REL;
    } else {
      node = cite(Number(found.cite), match[0]);
      if (!node) continue;
    }
    target.append(text.slice(last, match.index), node);
    last = match.index + match[0].length;
  }
  target.append(text.slice(last));
}

function mdTable(target, lines, start, inline) {
  const aligns = mdCells(lines[start + 1]).map((rule) => (rule.endsWith(":") ? (rule.startsWith(":") ? "center" : "right") : ""));
  let end = start + 2;
  while (end < lines.length && lines[end].trim() && lines[end].includes(CELL_MARK)) end++;
  const fill = (box, cell, column) => { box.className = aligns[column] || ""; inline(box, cell); };
  target.append(tableOf(mdCells(lines[start]), lines.slice(start + 2, end).map(mdCells), fill));
  return end;
}

// A table of rows of cells, with a heading row or none; fill(box, cell, column) writes a cell. A wide one scrolls inside its wrap.
function tableOf(heading, rows, fill) {
  const row = (cells, tag) => {
    const tr = el("tr");
    cells.forEach((cell, column) => { const box = el(tag); fill(box, cell, column); tr.append(box); });
    return tr;
  };
  const table = el("table"), body = el("tbody");
  if (heading) { const head = el("thead"); head.append(row(heading, "th")); table.append(head); }
  body.append(...rows.map((cells) => row(cells, "td")));
  table.append(body);
  const wrap = el("div", "table-wrap");
  wrap.append(table);
  return wrap;
}

// A source's text is the norm's own wording and is shown as it is written (it is not Markdown: a "(*)" in it is a footnote
// mark). Only its tables are drawn: they are kept as lines of cells with "|" between them, with no heading row marked.
function sourceText(text) {
  const box = el("div", "text");
  const lines = text.split("\n");
  let plain = [];
  const closePlain = () => { const part = plain.join("\n").replace(/^\n+|\n+$/g, ""); if (part) box.append(part); plain = []; };
  for (let i = 0; i < lines.length;) {
    let end = i;
    while (end < lines.length && lines[end].includes(CELL_MARK)) end++;
    const rows = lines.slice(i, end).map((line) => line.split(CELL_MARK).map((cell) => cell.trim()));
    if (rows.length >= MIN_TABLE_ROWS && rows.every((cells) => cells.length === rows[0].length)) {
      const used = rows[0].map((_, column) => rows.some((cells) => cells[column]));  // a column or a row with nothing in it is left out
      const kept = rows.map((cells) => cells.filter((_, column) => used[column])).filter((cells) => cells.some(Boolean));
      closePlain();
      if (kept.length) box.append(tableOf(null, kept, (cellBox, cell) => { cellBox.textContent = cell; }));
      i = end;
    } else {
      end = Math.max(end, i + 1);
      plain.push(...lines.slice(i, end));
      i = end;
    }
  }
  closePlain();
  return box;
}

function mdList(target, lines, start, inline) {
  const first = lines[start].match(MD_ITEM);
  const base = first[1].length, ordered = first[3] !== undefined;
  const sameList = (line) => { const m = line.match(MD_ITEM); return Boolean(m) && m[1].length === base && (m[3] !== undefined) === ordered; };
  const list = el(ordered ? "ol" : "ul");
  if (ordered && Number(first[3]) !== MD_LIST_START) list.start = Number(first[3]);
  let item = null, offset = 0, i = start;
  const closeItem = () => { if (!item) return; const li = el("li"); mdBlocks(li, item, inline); list.append(li); };
  for (; i < lines.length; i++) {
    const line = lines[i], indent = mdIndent(line);
    if (!line.trim()) {  // an empty line ends the list unless an item or a deeper line follows it
      const next = lines.slice(i + 1).find((later) => later.trim());
      if (next === undefined || !(mdIndent(next) > base || sameList(next))) break;
      item.push("");
    } else if (sameList(line)) {
      closeItem();
      const text = line.match(MD_ITEM)[4];
      item = [text]; offset = line.length - text.length;
    } else if (indent > base) {
      item.push(line.slice(Math.min(indent, offset)));  // of this item: a second paragraph, a list inside it
    } else if (lines[i - 1].trim() && !mdStartsBlock(lines, i)) {
      item.push(line.trim());  // the item's text going on, not indented
    } else break;
  }
  closeItem();
  target.append(list);
  return i;
}

function mdBlocks(target, lines, inline) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let match;
    if (!line.trim()) { i++; continue; }
    if ((match = line.match(MD_FENCE))) {
      const closes = new RegExp("^\\s*" + match[2][0] + "{" + match[2].length + ",}\\s*$");
      const code = [];
      for (i++; i < lines.length && !closes.test(lines[i]); i++) code.push(lines[i].slice(Math.min(mdIndent(lines[i]), match[1].length)));
      i++;  // past the closing line; a block still being written has none yet
      const pre = el("pre");
      pre.append(el("code", "", code.join("\n")));
      target.append(pre);
    } else if ((match = line.match(MD_HEADING))) {
      const heading = el("h" + Math.min(match[1].length + MD_HEADING_SHIFT, MD_MAX_HEADING));
      inline(heading, match[2]);
      target.append(heading);
      i++;
    } else if (mdIsTable(lines, i)) {
      i = mdTable(target, lines, i, inline);
    } else if (MD_RULE.test(line)) {
      target.append(el("hr"));
      i++;
    } else if (MD_QUOTE.test(line)) {
      const quoted = [];
      for (; i < lines.length && (match = lines[i].match(MD_QUOTE)); i++) quoted.push(match[1]);
      const quote = el("blockquote");
      mdBlocks(quote, quoted, inline);
      target.append(quote);
    } else if (MD_ITEM.test(line)) {
      i = mdList(target, lines, i, inline);
    } else {
      const text = [line.trim()];
      for (i++; i < lines.length && lines[i].trim() && !mdEndsParagraph(lines, i); i++) text.push(lines[i].trim());
      const paragraph = el("p");
      inline(paragraph, text.join("\n"));  // a line break in the answer stays one (white-space: pre-wrap)
      target.append(paragraph);
    }
  }
}

// Draws an answer: its Markdown, with each [n] that is a source made a link to it.
function addCitations(target, text, openSource, sourceCount) {
  const cite = (number, label) => {
    if (number < 1 || number > sourceCount) return null;
    const link = el("a", "cite", label);
    link.href = "#";
    link.addEventListener("click", (event) => { event.preventDefault(); openSource(number); });
    return link;
  };
  target.replaceChildren();
  try {
    mdBlocks(target, text.replace(/\r\n?/g, "\n").replaceAll("\t", MD_TAB).split("\n"), (node, part) => mdInline(node, part, cite));
  } catch (error) {  // an answer the reading of Markdown fails on is still shown, as it was written
    console.error(error);
    target.textContent = text;
  }
}

// searchCount: the searches of the answer so far. With more than one, a source's row says which search first gave it.
function renderSources(box, sources, searchCount) {
  const wasOpen = Boolean(box.querySelector("details")?.open);  // the list is drawn again after every search: it stays open if it was
  box.replaceChildren();
  if (!sources.length) return;
  const wrap = el("details");
  wrap.open = wasOpen;
  wrap.append(el("summary", "", "Sources (" + sources.length + ")"));
  for (const s of sources) {
    const item = el("details", "source");
    item.id = "src-" + box.dataset.id + "-" + s.number;
    const summary = el("summary");
    // an answer saved before sources had a citation has only the norm's code and year
    const name = s.reference || s.doc_code + " (" + s.year + ")";
    const score = s.score == null ? SOURCE_BY_NUMBER : SOURCE_SCORE + s.score;
    const search = searchCount > 1 && s.search ? SOURCE_SEARCH + s.search : "";
    const content = el("span", "summary-content", ["[" + s.number + "] " + name, s.pages, score, search].filter(Boolean).join(ROW_SEPARATOR));
    if (s.status_note) content.append(el("span", "badge warn", s.status_note));
    if (s.images.length) content.append(el("span", "badge", s.images.length + (s.images.length === 1 ? " image" : " images")));
    if (s.images.length) {
      for (const image of s.images) {
        const thumb = el("img", "thumb");
        thumb.src = image.url; thumb.loading = "lazy"; thumb.alt = image.caption || "figure";
        thumb.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openLightbox(image); });
        content.append(thumb);
      }
    }
    summary.append(content);
    item.append(summary);
    if (s.citation) item.append(el("div", "citation", s.citation));
    item.append(el("div", "crumb", s.breadcrumb));
    item.append(sourceText(s.text));
    if (s.images.length) {
      const pics = el("div", "pics");
      for (const image of s.images) {
        const caption = imageCaption(image);
        const figure = el("figure");
        const link = el("a");
        link.href = image.url;
        link.addEventListener("click", (event) => { event.preventDefault(); openLightbox(image); });
        const img = el("img");
        img.src = image.url; img.loading = "lazy"; img.alt = image.caption || "figure";
        link.append(img);
        figure.append(link);
        if (caption) figure.append(el("figcaption", "", caption));
        pics.append(figure);
      }
      item.append(pics);
    }
    wrap.append(item);
  }
  box.append(wrap);
}

// One search of the model's as a line: its number, what was asked (words, or the number of an article), the norm if it was
// limited to one, and what came of it.
function fmtSearch(search) {
  const asked = search.article ? SEARCH_ARTICLE + search.article : (search.query ? SEARCH_QUOTE + search.query + SEARCH_QUOTE : "");
  const fresh = search.new != null && search.new !== search.found ? SEARCH_NEW.replace("{count}", search.new) : "";
  const came = search.problem ? SEARCH_NOT_RUN + search.problem : search.found + (search.found === 1 ? SEARCH_RESULT : SEARCH_RESULTS) + fresh;
  return search.number + SEARCH_NUMBER_END + [asked, search.norm, came].filter(Boolean).join(ROW_SEPARATOR);
}

// The searches of an answer, above it: of one that is being written and of a saved one.
function renderSearches(box, searches) {
  box.replaceChildren();
  for (const search of searches) {
    const line = el("div", "search-line", fmtSearch(search));
    if (search.problem) line.classList.add("warn");
    box.append(line);
  }
}

// ---- the image viewer -----------------------------------------------------------------------------------------------
function imageCaption(image) {
  return (image.page ? "p. " + image.page + (image.caption ? " · " : "") : "") + (image.caption || "");
}

function openLightbox(image) {
  const box = $("lightbox");
  $("lightbox_img").src = image.url;
  $("lightbox_img").alt = image.caption || "figure";
  $("lightbox_caption").textContent = imageCaption(image);
  box.hidden = false;
  document.body.classList.add("viewing");
}

function closeLightbox() {
  $("lightbox").hidden = true;
  $("lightbox_img").removeAttribute("src");
  document.body.classList.remove("viewing");
}

let messageCount = 0;

function newBotMessage() {
  const id = ++messageCount;
  const root = el("div", "msg bot");
  const status = el("div", "status");
  const label = el("span", "", "");
  const bar = el("progress");
  bar.max = 1; bar.hidden = true;
  status.append(label, bar);
  const thinking = el("details", "thinking");
  thinking.hidden = true;
  const thinkingSummary = el("summary", "", "Thinking");
  const thinkingText = el("pre");
  thinking.append(thinkingSummary, thinkingText);
  const searches = el("div", "searches");
  const answer = el("div", "answer");
  const sources = el("div");
  sources.dataset.id = id;
  const errors = el("div");
  const stats = el("div", "stats");
  root.append(status, thinking, searches, answer, errors, sources, stats);
  return { root, label, bar, thinking, thinkingSummary, thinkingText, searches, answer, sources, errors, stats, id };
}

function renderSavedBotMessage(box, msg) {
  const id = ++messageCount;
  const root = el("div", "msg bot");
  const status = el("div", "status");
  status.append(el("span", "", msg.errors && msg.errors.length ? "Failed" : (msg.content && msg.content.trim() ? "Done" : "No answer")));
  const searched = msg.searches || [];  // an answer saved before the model could search has none
  const searches = el("div", "searches");
  renderSearches(searches, searched);
  const answer = el("div", "answer");
  const sources = el("div");
  sources.dataset.id = id;
  const errors = el("div");
  const stats = el("div", "stats");
  root.append(status, searches, answer, errors, sources, stats);

  if (msg.content) {
    const sourceCount = msg.sources ? msg.sources.length : 0;
    const openSource = (number) => {
      const item = document.getElementById("src-" + id + "-" + number);
      if (!item) return;
      sources.querySelector("details").open = true;
      item.open = true;
      item.scrollIntoView({ block: "nearest" });
      item.classList.add("flash");
      setTimeout(() => item.classList.remove("flash"), 1200);
    };
    addCitations(answer, msg.content, openSource, sourceCount);
  }
  if (msg.sources && msg.sources.length) renderSources(sources, msg.sources, searched.length);
  if (msg.stats) stats.textContent = fmtStats(msg.stats);
  for (const note of (msg.notices || [])) errors.append(el("div", "stats", note));
  for (const err of (msg.errors || [])) errors.append(el("div", "err", err));
  box.append(root);
}

function parseEvents(buffer) {
  const events = [];
  let rest = buffer;
  let cut;
  while ((cut = rest.indexOf("\n\n")) !== -1) {
    const block = rest.slice(0, cut);
    rest = rest.slice(cut + 2);
    let name = "message", data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (data) { try { events.push([name, JSON.parse(data)]); } catch (e) { /* a broken event is skipped */ } }
  }
  return [events, rest];
}

function fmtStats(stats) {
  const parts = [];
  const about = stats.approximate ? "~" : "";
  if (stats.total_output_tokens != null) parts.push(about + stats.total_output_tokens + " tokens" + (stats.reasoning_output_tokens ? " (" + about + stats.reasoning_output_tokens + " thinking)" : ""));
  if (stats.tokens_per_second != null) parts.push(stats.tokens_per_second.toFixed(1) + " tokens/s");
  if (stats.time_to_first_token_seconds != null) parts.push("first token after " + stats.time_to_first_token_seconds.toFixed(2) + " s");
  if (stats.input_tokens != null) parts.push(stats.input_tokens + " tokens read");
  return parts.join(" · ");
}

async function ask(question) {
  const conversation = currentConversation;  // the answer belongs here even if another chat is opened while it streams
  const box = $("chat");
  $("empty")?.remove();
  box.append(el("div", "msg user", question));
  const bot = newBotMessage();
  box.append(bot.root);
  box.scrollTop = box.scrollHeight;

  const history = historyForModel();
  const body = {
    question, history, model: $("model").value.trim(), top: Number($("top").value), doc: $("doc").value,
    embedding_server: $("embedding_server").value.trim(), chat_server: $("chat_server").value.trim(),
    chat_api: $("chat_api").value, chat_path: $("chat_path").value.trim(), chat_api_key: $("chat_api").value === "openai" ? $("chat_api_key").value.trim() : "",
    search: $("chat_api").value === "openai" ? $("search_mode").value : SEARCH_ONCE,
  };
  let rawAnswer = "", answerText = "", reasoningText = "", reasoningParts = 0, sourceCount = 0, finished = false, failed = false;
  let earlierThinking = "";  // what the model thought before its last search: an inline <think> block of a later round is added to it
  let savedSources = null, savedStats = null, savedErrors = [], savedNotices = [], savedSearches = [];
  const showProgress = (text, fraction) => { bot.label.textContent = text + " " + Math.round(fraction * 100) + "%"; bot.bar.hidden = false; bot.bar.value = fraction; };
  const showThinking = () => {
    bot.thinking.hidden = false;
    bot.thinkingSummary.textContent = "Thinking · " + reasoningParts + " tokens";
    bot.thinkingText.textContent = reasoningText;
  };
  const openSource = (number) => {
    const item = document.getElementById("src-" + bot.id + "-" + number);
    if (!item) return;
    bot.sources.querySelector("details").open = true;
    item.open = true;
    item.scrollIntoView({ block: "nearest" });
    item.classList.add("flash");
    setTimeout(() => item.classList.remove("flash"), 1200);
  };
  const showAnswer = () => addCitations(bot.answer, answerText, openSource, sourceCount);
  const showSources = (sources) => {  // all of the answer so far, or null for none
    savedSources = sources;
    sourceCount = sources ? sources.length : 0;
    renderSources(bot.sources, sources || [], savedSearches.length);
  };
  const showNotice = (message) => { savedNotices.push(message); bot.errors.append(el("div", "stats", message)); };
  // The model is asked again after a search: what it wrote so far was a draft and goes, what it thought stays.
  const endRound = () => {
    bot.bar.hidden = true;
    rawAnswer = answerText = "";
    showAnswer();
    if (reasoningText && !reasoningText.endsWith(THINKING_BREAK)) reasoningText += THINKING_BREAK;
    earlierThinking = reasoningText;
  };
  const handle = (name, data) => {
    const stick = nearBottom(box);
    if (name === "status") {
      bot.label.textContent = data.state === "searching" ? STATUS_SEARCHING : STATUS_WAITING;
    } else if (name === "search") {
      savedSearches.push(data);
      endRound();
      renderSearches(bot.searches, savedSearches);
      if (savedSources) showSources(savedSources);  // from the second search on, their rows say which search gave them
      bot.label.textContent = data.problem ? STATUS_WAITING : STATUS_READING_SEARCH.replace("{number}", data.number);
    } else if (name === "sources") {
      showSources(data.sources);
    } else if (name === "fallback") {  // the model did not search as it should: the server searches once itself and asks again
      endRound();
      showSources(null);
      showNotice(data.message);
    } else if (name === "model_load") {
      showProgress("Loading the model…", data.progress);
    } else if (name === "prompt") {
      showProgress("Reading the prompt…", data.progress);
    } else if (name === "reasoning") {
      bot.bar.hidden = true;
      reasoningText += data.content; reasoningParts += 1;
      bot.label.textContent = "Thinking… " + reasoningParts + " tokens";
      showThinking();
    } else if (name === "message") {
      bot.bar.hidden = true;
      rawAnswer += data.content;
      const inline = rawAnswer.match(THINK_BLOCK);
      if (inline) { reasoningText = earlierThinking + inline[1]; reasoningParts = reasoningText.length ? Math.max(reasoningParts, 1) : reasoningParts; showThinking(); }
      answerText = inline ? rawAnswer.slice(inline[0].length) : rawAnswer;
      bot.label.textContent = inline && !inline[2] ? "Thinking…" : "Writing the answer…";
      showAnswer();
    } else if (name === "notice") {
      showNotice(data.message);
    } else if (name === "done") {
      savedStats = data.stats || {};
      finished = true;
      bot.bar.hidden = true;
      bot.stats.textContent = fmtStats(data.stats || {});
      if (failed) return;
      bot.label.textContent = answerText.trim() ? "Done" : "No answer";
      const stats = data.stats || {};
      if (answerText.trim() && stats.cut) {
        bot.errors.append(el("div", "err", "The answer is cut short: the model ran out of room (its context or output limit). Do not rely on it as it stands."));
      } else if (answerText.trim() && stats.incomplete) {
        bot.errors.append(el("div", "err", "The stream ended without a finish marker, so the answer may be incomplete."));
      }
      if (!answerText.trim()) {
        bot.errors.append(el("div", "err", "The model gave no answer. A model that thinks at length can use up its room before it starts to answer: load it with a larger context length, or pick another model."));
      }
    } else if (name === "error") {
      savedErrors.push(data.message);
      failed = true;
      bot.bar.hidden = true;
      bot.label.textContent = "Failed";
      bot.errors.append(el("div", "err", data.message));
    }
    if (stick) box.scrollTop = box.scrollHeight;
  };

  controller = new AbortController();
  try {
    const reply = await fetch("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal,
    });
    if (!reply.ok) {
      const problem = await reply.json().catch(() => ({}));
      handle("error", { message: problem.error || "The server answered " + reply.status });
    } else {
      const reader = reply.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const [events, rest] = parseEvents(buffer);
        buffer = rest;
        for (const [name, data] of events) handle(name, data);
      }
      if (!finished && !failed) handle("error", { message: "The connection ended before the answer was complete." });
    }
  } catch (error) {
    if (error.name === "AbortError") {
      bot.label.textContent = "Stopped"; bot.bar.hidden = true;
      savedErrors.push("Stopped.");
    } else {
      handle("error", { message: "Lost the connection: " + error.message });
    }
  }

  conversation.messages.push({ role: "user", content: question });
  conversation.messages.push({
    role: "bot", content: answerText, sources: savedSources, stats: savedStats, errors: savedErrors, notices: savedNotices, searches: savedSearches,
  });
  if (!conversation.title) conversation.title = question.slice(0, 50);
  if (!conversation.created) conversation.created = new Date().toISOString();
  saveToHistory(conversation);
  renderHistory();

  controller = null;
}

function setBusy(busy) {
  $("send").hidden = busy; $("stop").hidden = !busy; $("question").disabled = busy;
  if (!busy) $("question").focus();
}

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || controller) return;
  saveSettings();
  $("question").value = "";
  $("question").style.height = "";
  setBusy(true);
  try { await ask(question); } finally { setBusy(false); }
});
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); }
});
$("question").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = Math.min(event.target.scrollHeight, MAX_INPUT_PX) + "px";
});
$("stop").addEventListener("click", () => controller && controller.abort());
$("refresh").addEventListener("click", () => { saveSettings(); loadModels(); });
for (const id of ["chat_server", "chat_api"]) $(id).addEventListener("change", () => { $("model").value = ""; showUsualPath(); saveSettings(); loadModels(); });
$("chat_api_key").addEventListener("change", () => { saveSettings(); loadModels(); });
for (const id of ["embedding_server", "chat_path", "top", "doc", "model", "search_mode"]) $(id).addEventListener("change", saveSettings);
for (const id of ["chat_server", "chat_api", "chat_path"]) $(id).addEventListener("input", showUsualPath);
$("showkey").addEventListener("click", () => {
  const hidden = $("chat_api_key").type === "password";
  $("chat_api_key").type = hidden ? "text" : "password";
  $("showkey").textContent = hidden ? "Hide" : "Show";
  $("showkey").setAttribute("aria-pressed", String(hidden));
});
$("forgetkey").addEventListener("click", () => { $("chat_api_key").value = ""; store.remove("chat_api_key"); loadModels(); });
$("lightbox").addEventListener("click", (event) => { if (event.target === $("lightbox")) closeLightbox(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("lightbox").hidden) closeLightbox(); });
$("toggle").addEventListener("click", () => $("settings").classList.toggle("hidden"));
$("sidebar-toggle").addEventListener("click", () => $("sidebar").classList.toggle("collapsed"));
function showEmptyState() {
  const hint = el("div", "empty");
  hint.id = "empty";
  const examples = el("div", "examples");
  for (const example of EMPTY_EXAMPLES) {
    const button = el("button", "example", example);
    button.type = "button";
    button.lang = EXAMPLES_LANGUAGE;
    button.addEventListener("click", () => {
      $("question").value = example;
      $("question").dispatchEvent(new Event("input"));
      $("question").focus();
    });
    examples.append(button);
  }
  hint.append(el("div", "empty-title", EMPTY_TITLE), el("div", "empty-hint", EMPTY_HINT), examples);
  $("chat").replaceChildren(hint);
}
for (const id of ["new-chat", "clear"]) {  // the sidebar's and the header's: a running answer is stopped first
  $(id).addEventListener("click", () => {
    if (controller) controller.abort();
    newConversation();
  });
}

closeSidebarOnNarrowScreen();
showEmptyState();
renderHistory();
{ // load the most recent conversation on page load, if any
  const history = loadHistory();
  if (history.length) loadConversation(history[0].id);
}

if (!store.works()) $("keyNote").textContent = "This browser blocks storage for this page: nothing is kept between visits, including the API key.";
loadSettings().catch((error) => { $("modelsNote").className = "note bad"; $("modelsNote").textContent = "Could not load the settings: " + error.message; });
