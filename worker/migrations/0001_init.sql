-- The chunks the search serves, loaded from the Mac after chunking. Only chunks that get a vector are kept
-- (embed_chunks.texts_to_embed): those are the ones search.py searches and fetches by number.
--
-- Loading a norm again (after a re-chunk), in one transaction (one --file or db.batch):
--   1. read its figures' (id, sha256), to upload only the PNGs that changed;
--   2. upsert its norms row with INSERT ... ON CONFLICT (doc_key) DO UPDATE (never INSERT OR REPLACE: that deletes
--      the row, and with it, through the cascades below, all its chunks);
--   3. DELETE FROM chunks WHERE doc_key = ?; DELETE FROM figures WHERE doc_key = ?  (the links go with them);
--   4. insert its chunks, article numbers, figures and links.
-- Load only the documents that are chunked: chunk-config.json also holds older entries that share their codes.

-- One row per norm: the fields of its chunk-config.json entry that every chunk carried. Take them from the config,
-- not from chunk files made before a code changed.
CREATE TABLE norms (
  doc_key      TEXT PRIMARY KEY,           -- the name of the document, as in chunk-config.json
  -- "P118/2-2013": what is cited, and the Vectorize filter value. Unique, so that two norms can never be searched as one.
  code         TEXT NOT NULL UNIQUE,
  title        TEXT NOT NULL,
  edition_year INTEGER NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('in_force', 'unverified', 'repealed')),
  amended_by   TEXT NOT NULL DEFAULT '[]', -- JSON list of acts
  repealed_on  TEXT,                       -- ISO date
  gazette      TEXT
);

-- One row per chunk. Its id is also its vector's id in Vectorize, which takes at most 64 bytes: json_to_chunks.py
-- makes it from the norm's code, so a short code in chunk-config.json ("P118/2-2013") keeps it well under.
CREATE TABLE chunks (
  id           TEXT PRIMARY KEY CHECK (length(CAST(id AS BLOB)) <= 64),
  doc_key      TEXT NOT NULL REFERENCES norms (doc_key) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,           -- order within the norm
  kind         TEXT NOT NULL,
  heading_path TEXT NOT NULL DEFAULT '[]', -- JSON list
  label        TEXT,
  caption      TEXT,
  article      TEXT,
  articles     TEXT NOT NULL DEFAULT '[]', -- JSON list, as in the chunk record
  pdf_pages    TEXT,                       -- JSON list, or NULL
  text         TEXT NOT NULL,
  embed_text   TEXT NOT NULL,              -- the text the vector was made from; an excerpt shows it
  -- Kept as the chunk record has them, with no foreign key: they may name chunks that are not embedded, so not here.
  prev_id      TEXT,
  next_id      TEXT,
  parent_id    TEXT,
  flags        TEXT NOT NULL DEFAULT '[]'  -- JSON list
);
CREATE INDEX chunks_by_norm ON chunks (doc_key, seq);

-- A chunk can hold several article numbers: one row each, for the lookup by number. The loader drops repeats.
CREATE TABLE chunk_articles (
  chunk_id TEXT NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
  number   TEXT NOT NULL,                  -- "2.56", as search.article_number gives it
  PRIMARY KEY (chunk_id, number)
);
CREATE INDEX chunk_articles_by_number ON chunk_articles (number, chunk_id);

-- The pictures: one row each, linked to every chunk that lists it.
CREATE TABLE figures (
  id       TEXT PRIMARY KEY,               -- the norm's code slug and Docling's picture number: "p118-2-2013/fig-12"
  doc_key  TEXT NOT NULL REFERENCES norms (doc_key) ON DELETE CASCADE,
  r2_key   TEXT NOT NULL,                  -- the PNG's key in the bucket (its file name holds its own hash)
  -- Of the PNG, so that an upload sends only what changed; '' when the upload had no --bucket (it matches no file,
  -- so a later upload with a bucket sends that PNG).
  sha256   TEXT NOT NULL,
  page     INTEGER,
  width    INTEGER NOT NULL,
  height   INTEGER NOT NULL,
  caption  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX figures_by_norm ON figures (doc_key);

-- The loader drops a figure listed twice on one chunk.
CREATE TABLE chunk_figures (
  chunk_id  TEXT NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
  figure_id TEXT NOT NULL REFERENCES figures (id) ON DELETE CASCADE,
  position  INTEGER NOT NULL,              -- order in the chunk's `images`
  PRIMARY KEY (chunk_id, figure_id)
);
CREATE INDEX chunk_figures_by_figure ON chunk_figures (figure_id);
