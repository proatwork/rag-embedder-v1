// get_figure: one figure handed to the model as an image, with what it is and its link.
import { describe, expect, it } from "vitest";
import {
  base64,
  figureHeader,
  loadFigure,
  MAX_FIGURE_BYTES,
  NO_FIGURE_ID,
  PNG_MEDIA_TYPE,
  TOO_LARGE,
  TOO_LARGE_NO_LINK,
} from "../src/figure";
import { format } from "../src/search";
import type { FigureFile } from "../src/types";
import { FakeStore } from "./fakes";

const URL = "https://figures.example.test/p118-2-2013/fig-12.png";
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function file(fields: Partial<FigureFile> = {}): FigureFile {
  return {
    figure: { id: "p118-2-2013/fig-12", url: URL, page: 36, width: 640, height: 240, caption: "Figura 12\nHidrant" },
    code: "P118/2-2013",
    size: PNG.length,
    bytes: PNG.buffer,
    ...fields,
  };
}

function storeWith(...files: FigureFile[]) {
  return new FakeStore([], new Map(files.map((one) => [one.figure.id, one])));
}

describe("base64", () => {
  it("encodes bytes as btoa would, also past one run of bytes", () => {
    expect(base64(PNG.buffer)).toBe("iVBORw0KGgo=");
    const big = new Uint8Array(0x8000 * 2 + 3).map((_, place) => place % 251);
    expect(atob(base64(big.buffer))).toBe(Array.from(big, (byte) => String.fromCharCode(byte)).join(""));
  });
});

describe("figureHeader", () => {
  it("says which figure, of which norm, on which page, its caption and its link", () => {
    expect(figureHeader(file())).toBe(
      `Figure p118-2-2013/fig-12 · P118/2-2013 (PDF p. 36) — "Figura 12 Hidrant"\n${URL}`,
    );
  });

  it("leaves out what is not known", () => {
    const bare = file({ figure: { ...file().figure, url: "", page: null, caption: "" } });
    expect(figureHeader(bare)).toBe("Figure p118-2-2013/fig-12 · P118/2-2013");
  });
});

describe("loadFigure", () => {
  it("hands over the PNG as an image under its header", async () => {
    const store = storeWith(file());
    const reply = await loadFigure(store, { id: " p118-2-2013/fig-12 " });
    expect(reply).toEqual({
      text: figureHeader(file()),
      image: { data: "iVBORw0KGgo=", mimeType: PNG_MEDIA_TYPE },
      isError: false,
    });
    expect(store.calls).toEqual([{ method: "figure", args: ["p118-2-2013/fig-12", MAX_FIGURE_BYTES] }]);
  });

  it("gives only the link of a figure too large to hand over", async () => {
    const reply = await loadFigure(storeWith(file({ size: MAX_FIGURE_BYTES + 1 })), { id: "p118-2-2013/fig-12" });
    expect(reply).toEqual({
      text: figureHeader(file()) + "\n" + format(TOO_LARGE, { size: MAX_FIGURE_BYTES + 1 }),
      image: null,
      isError: false,
    });
  });

  it("does not ask for a link that a too large figure does not have", async () => {
    const bare = file({ size: MAX_FIGURE_BYTES + 1, figure: { ...file().figure, url: "" } });
    const reply = await loadFigure(storeWith(bare), { id: "p118-2-2013/fig-12" });
    expect(reply.text).toBe(figureHeader(bare) + "\n" + format(TOO_LARGE_NO_LINK, { size: MAX_FIGURE_BYTES + 1 }));
    expect(reply.text).not.toContain("link instead");
  });

  it("gives the whole caption, which an excerpt cuts", async () => {
    const long = file({ figure: { ...file().figure, caption: "x".repeat(200) } });
    expect((await loadFigure(storeWith(long), { id: "p118-2-2013/fig-12" })).text).toContain("x".repeat(200));
  });

  it("is an error that says why for no id, an id of another type, or an unknown figure", async () => {
    for (const args of [{}, { id: 12 }, { id: "  " }]) {
      expect(await loadFigure(storeWith(), args)).toEqual({ text: NO_FIGURE_ID, image: null, isError: true });
    }
    const unknown = await loadFigure(storeWith(), { id: "x/fig-9" });
    expect(unknown.isError).toBe(true);
    expect(unknown.text).toContain('"x/fig-9"');
    expect(unknown.image).toBeNull();
  });
});
