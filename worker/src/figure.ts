// The second tool, get_figure(id): one figure of the norms handed to the model as an image, so that it can see what
// an excerpt's FIGURE_LINE points to. Only the Worker has it; mcp_server.py has no figures to give.
import { LINE_SEPARATOR, format, pagesNote, pyStrip, shortened } from "./search";
import { FIGURE_CAPTION, FIGURE_LINE_PREFIX, FIGURE_PAGE, TOOL_NAME } from "./tool";
import type { FigureFile, Store } from "./types";

export const FIGURE_TOOL_NAME = "get_figure";
export const FIGURE_TOOL_TITLE = "Load a figure of the fire-safety norms";
export const ARG_FIGURE_ID = "id";
export const FIGURE_TOOL_DESCRIPTION =
  "Load a figure (a picture or diagram) of the Romanian fire-safety norms, to see what it shows. " +
  `Give the \`${ARG_FIGURE_ID}\` that a ${TOOL_NAME} excerpt lists on its "${FIGURE_LINE_PREFIX} <id> …" line. ` +
  "Returns the picture, with its norm, page, caption and link.";
export const FIGURE_ID_HELP = `A figure's id as a ${TOOL_NAME} excerpt lists it, for example "p118-2-2013/fig-12".`;
// The largest PNG handed over: Claude takes images up to 5 MB, and the largest figure of the norms is about 360 KB.
export const MAX_FIGURE_BYTES = 1_000_000;
export const PNG_MEDIA_TYPE = "image/png";
// Of the caption over a loaded figure: the whole of any the norms have (an excerpt cites only the start of it).
export const FIGURE_CAPTION_CHARS = 1000;

export const FIGURE_HEADER = FIGURE_LINE_PREFIX + " {id} · {code}{page}{caption}";
export const NO_FIGURE_ID = `Give \`${ARG_FIGURE_ID}\`: the id of a figure, as a ${TOOL_NAME} excerpt lists it.`;
export const NO_SUCH_FIGURE =
  `There is no figure {id} to load. Figure ids are listed under the citation of a ${TOOL_NAME} excerpt, ` +
  `on its "${FIGURE_LINE_PREFIX} <id> …" lines.`;
export const TOO_LARGE = "The picture is too large to load here ({size} bytes): give the user its link instead.";
export const TOO_LARGE_NO_LINK = "The picture is too large to load here ({size} bytes), and it has no public link.";

const BINARY_RUN = 0x8000; // bytes turned into one string at a time, under the limit of a call's arguments

/** What get_figure answers: text, and the picture when it could be loaded. */
export interface FigureReply {
  text: string;
  image: { data: string; mimeType: string } | null;
  isError: boolean;
}

/** `bytes` in base64, as MCP carries an image. */
export function base64(bytes: ArrayBuffer): string {
  const view = new Uint8Array(bytes);
  let binary = "";
  for (let start = 0; start < view.length; start += BINARY_RUN) {
    binary += String.fromCharCode(...view.subarray(start, start + BINARY_RUN));
  }
  return btoa(binary);
}

/** The lines over the picture: what figure it is, of which norm, on which page, its caption, and its link. */
export function figureHeader(file: FigureFile): string {
  const page = pagesNote([file.figure.page]);
  const caption = shortened(file.figure.caption, FIGURE_CAPTION_CHARS);
  const header = format(FIGURE_HEADER, {
    id: file.figure.id,
    code: file.code,
    page: page ? format(FIGURE_PAGE, { page }) : "",
    caption: caption ? format(FIGURE_CAPTION, { caption }) : "",
  });
  return file.figure.url ? header + LINE_SEPARATOR + file.figure.url : header;
}

/** One call of get_figure. Throws what the store throws. */
export async function loadFigure(store: Store, args: Readonly<Record<string, unknown>>): Promise<FigureReply> {
  const value = args[ARG_FIGURE_ID];
  const id = typeof value === "string" ? pyStrip(value) : "";
  if (!id) return { text: NO_FIGURE_ID, image: null, isError: true };
  const file = await store.figure(id, MAX_FIGURE_BYTES);
  if (!file) return { text: format(NO_SUCH_FIGURE, { id: JSON.stringify(id) }), image: null, isError: true };
  if (!file.bytes) {
    return {
      text:
        figureHeader(file) +
        LINE_SEPARATOR +
        format(file.figure.url ? TOO_LARGE : TOO_LARGE_NO_LINK, { size: file.size }),
      image: null,
      isError: false,
    };
  }
  return { text: figureHeader(file), image: { data: base64(file.bytes), mimeType: PNG_MEDIA_TYPE }, isError: false };
}
