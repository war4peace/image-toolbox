# Content-hash lineage (0.2.1-experimental)

> **Status: implemented.** A dedicated `lineage` table holds the three linked
> hashes; a shared `file_hashes` table memoises content hashes (blake2b-256) by
> `path + mtime + size`. The upscaler records `src_hash → upscaled_hash`; tag &
> rename records `upscaled_hash(in) → tagged_hash`; conciliation matches an
> original to its processed counterpart by hashing the original, looking up its
> final lineage hash, and locating that hash in the (freshly hashed) processed
> tree — falling back to mirrored-name matching when no lineage exists. See
> `db.py` (`content_hash`, `hash_file_cached`, `record_upscale_lineage`,
> `record_tag_lineage`, `lineage_final_hash`). The original research follows.


**Question:** Can each source photo be hashed into a unique fingerprint in the
database, the upscale result hashed and linked to it, and the same done for tag
& rename — so a source/processed pair can still be reconciled after the user
*moves or renames folder paths* (e.g. relocates `__upscaled__`)?

**Short answer:** Yes, and it's the right fix for the move-survival problem. But
the design hinges on one fact that rules out the "obvious" approach.

---

## The governing fact: each stage produces a *new, unrelated* hash

A lineage is three physical files:

| Stage | File | Content hash |
|-------|------|--------------|
| Source | original photo | `H0` |
| Upscaled | `output_root/<rel>` | `H1` |
| Tagged & renamed | final file | `H2` |

`H0`, `H1`, `H2` are **mutually underivable**:

- **Upscale rewrites and is non-deterministic.** [upscale_engine.py:206](../scripts/upscale_engine.py#L206)
  loads via PIL, applies EXIF orientation, runs SeedVR2 with a fresh
  `random.randint` seed every call ([upscale_engine.py:223](../scripts/upscale_engine.py#L223)),
  then re-encodes (JPEG q95, etc.). Re-upscaling the *same* source yields a
  *different* `H1`. So `H1 ≠ f(H0)`.
- **Tag & rename rewrites EXIF in place.** It writes a description into EXIF and
  re-snapshots ([tag_and_rename.py:1021](../scripts/tag_and_rename.py#L1021)), changing
  the bytes, so `H2 ≠ H1`.

**Consequence:** hashing alone cannot *link* the files — you cannot hash an
original and compute where its upscaled twin is. The link must be **stored
explicitly in the DB at the moment each stage runs** (when both the input and
output files are in hand). The hash's only — but decisive — role is to
**re-identify a physical file by content after its path has changed.**

This is exactly what fixes the user's scenario: paths become irrelevant;
identity travels with the bytes.

---

## How reconciliation works with hashes (the target flow)

User has moved `__upscaled__` somewhere new and points conciliation at a new
(source, processed) pair:

1. Walk the **source** tree; for each image compute/lookup `H0`.
2. `H0 → DB → H2` (the stored lineage gives the final processed file's hash and
   its recorded name).
3. Walk the **processed** tree once, building `{ content_hash : path }`.
4. Match `H2` to a real file in that index → that's the counterpart, wherever it
   now lives and whatever the folder is now called. Move it in.

No `root_id`/absolute-path lookup, no rel-path mirroring assumption, no
dependence on folder names. It also recovers renames that today live only in the
(path-keyed, move-fragile) tag cache.

---

## Why the current scheme breaks on a move (recap)

- `upscale_roots` / `tag_roots` are keyed by **absolute `source_root`**
  ([db.py:101](../scripts/db.py#L101), [db.py:118](../scripts/db.py#L118)). Move the tree and the
  root lookup misses entirely → the rename map in `tag_files` is lost.
- Conciliation then falls back to **mirrored rel-path matching**
  ([conciliate.py:163](../scripts/conciliate.py#L163)). That survives moving a *whole*
  tree intact, but breaks the moment relative structure, folder names, or file
  names diverge between the two trees — and it can never recover a rename.

Content hashing removes all of these dependencies.

---

## Cost & the hashing primitive

Full-file hashing of large photo trees is disk-bound, so:

- **Algorithm:** `hashlib.blake2b` — in the **standard library** (keeps the
  dependency-light promise), and markedly faster than SHA-256. No new packages.
  (`hashlib` is already imported across the codebase, only for hashing *paths*
  today — [batch_upscale.py:240](../scripts/batch_upscale.py#L240) etc.)
- **Make it incremental.** Cache `(content_hash)` next to the existing
  `(mtime, size)` fingerprint and only re-hash when the fingerprint changes. The
  upscale cache already stores `mtime`+`size` per file
  ([batch_upscale.py:354](../scripts/batch_upscale.py#L354)) — hashing slots in beside it
  at near-zero marginal cost on warm runs.
- **Hashing happens where the file is already being read.** The upscaler already
  loads each source ([upscale_engine.py:206](../upscale_engine.py#L206)) and
  writes each output; hash at those points instead of a separate pass.
- **Optional speed valve:** a sampled fingerprint (size + blake2b of head+tail
  blocks). Faster, microscopic collision risk. Recommend **full-file** for the
  archival use-case unless trees are huge; expose as a setting if needed.

---

## Proposed schema changes (additive, back-compatible)

Add nullable hash columns; old rows simply have `NULL` until next processed.

```sql
ALTER TABLE upscale_files ADD COLUMN src_hash TEXT;   -- H0, hash of the source
ALTER TABLE upscale_files ADD COLUMN out_hash TEXT;   -- H1, hash of the upscaled output
ALTER TABLE upscale_files ADD COLUMN out_rel_path TEXT; -- mirrored output rel path (explicit)

-- tag_files entries are keyed by original_rel_path (the upscaled name = stage-1 output)
ALTER TABLE tag_files ADD COLUMN in_hash  TEXT;   -- == upscale_files.out_hash (H1): the join key
ALTER TABLE tag_files ADD COLUMN out_hash TEXT;   -- H2, hash of the final tagged+renamed file
```

`tag_files.in_hash == upscale_files.out_hash` is the **content-level join** that
stitches the two previously-independent cache domains together (today they are
only stitched by a filename convention at read time — see the 0.2.0 findings).
Indexes on `src_hash` / `out_hash` make the reconciliation lookups O(1).

Because hashes are stored at **schema bump**, gate this behind the existing
cache-schema versioning in tag & rename (and add equivalent versioning to the
upscale cache) so a fresh DB or a re-scan populates them.

---

## Hook points (where to compute & store)

- **Source `H0` + upscaled `H1`:** in `run_pass` right after a successful
  `ENGINE.upscale(...)` and `cache.mark_done(...)`
  ([batch_upscale.py:964](../scripts/batch_upscale.py#L964)-[979](../scripts/batch_upscale.py#L979)),
  where both `local_path` and `out_path` exist. Store `src_hash`, `out_hash`,
  `out_rel_path` on the `upscale_files` row.
- **Tag input `H1` + final `H2`:** in `update_cache_entry`
  ([tag_and_rename.py:1005](../scripts/tag_and_rename.py#L1005)) after the final rename,
  hashing the file before write (= input) and after (= output). Persisted via
  `save_cache` ([tag_and_rename.py:913](../scripts/tag_and_rename.py#L913)) into the new
  columns alongside `entry_json`.

---

## Optional redundancy: embed `H0` in the output's EXIF

Tag & rename already stows the original filename in `XPComment`
([tag_and_rename.py:858](../scripts/tag_and_rename.py#L858)). The source hash `H0` could
likewise be written into a dedicated EXIF field on the **upscaled** (and tagged)
file. Then lineage survives even a **total DB loss** — conciliation could read
`H0` straight from the processed file.

Caveat: only JPEG carries EXIF cleanly across our save paths; PNG/WebP/BMP
([upscale_engine.py:148](../upscale_engine.py#L148)) do not, so EXIF is a
*best-effort breadcrumb*, not the primary store. **Recommendation: DB is the
source of truth; EXIF embedding is a cheap, format-permitting bonus.**

---

## Edge cases to handle

- **Duplicate sources (same bytes, different files):** `H0` is not unique across
  the tree. Key lineage by `(src_hash, source_rel_path)`, or accept that
  identical originals are interchangeable for reconciliation (usually fine).
- **Re-processing / force mode:** a new upscale overwrites the output (atomic
  replace) with new bytes → must **refresh** `out_hash`. Tie hash refresh to the
  same `already_done` / fingerprint-change logic so stale hashes can't linger.
- **Hash-then-edit races:** hash the upscaled file *after* the atomic
  `os.replace` ([upscale_engine.py:173](../upscale_engine.py#L173)), never the
  temp file.
- **Collisions:** blake2b-256 collision risk is negligible; pairing the hash
  with `size` makes it effectively nil.

---

## Recommendation

Proceed. Implement in this order:

1. **`db.py`:** add the columns + a `content_hash(path)` helper (blake2b,
   chunked read) and small upsert helpers. Schema-version the upscale cache.
2. **`batch_upscale.py`:** populate `src_hash`/`out_hash`/`out_rel_path`,
   fingerprint-gated.
3. **`tag_and_rename.py`:** populate `in_hash`/`out_hash` in the entry.
4. **`conciliate.py`:** add a hash-first matching path (build the processed-tree
   hash index, match `H0 → H2`), keeping mirrored-name matching as the fallback
   when hashes are absent (old data).
5. *(Optional)* EXIF breadcrumb for JPEG outputs.

This keeps every change additive and dependency-light, preserves the "never
touch originals / fail safe" philosophy (reconciliation only ever *gains*
matches it can prove by content), and makes folder moves a non-event.
