"""
Retro-fitting the model tag onto pre-0.6.3 remote learned-batch rows (#26 Part A).

The remote learn key was the bare RunPod card id, which held only while one pod ran one
DiT. 0.6.3 ended that, and the re-key ORPHANS every pre-existing remote row. Those rows are
converged batches measured on rented hardware, so they are worth rescuing, but only where
the install's own data proves which model produced them.

The rule under test is therefore "migrate on EVIDENCE, never on a guess", and the tests are
written around the ways a guess would go wrong. A blanket "assume 7B FP16" would be right on
most installs and wrong on any whose local card is 16 GB, because the wizard writes
FP8-mixed into `video.dit_model` for exactly those. And getting it wrong is not a mild
mis-seed: a learned value legitimately bypasses BATCH_CAP, so a ceiling filed against the
wrong model can OOM a real run.
"""

import db


def _learn(conn, gpu, mp_key, batch, when="2026-07-18T10:00:00"):
    conn.execute("INSERT OR REPLACE INTO video_batch_learn (gpu_id, mp_key, batch, updated_at)"
                 " VALUES (?,?,?,?)", (gpu, mp_key, batch, when))


def _probe(conn, gpu, model, batch=5, outcome="ok"):
    db.record_bench_probe(conn, gpu, model, 1920, 1080, batch, outcome,
                          frames=37, seconds=10.0)


def _keys(conn):
    return {r[0] for r in conn.execute("SELECT gpu_id FROM video_batch_learn")}


def test_an_orphaned_remote_key_is_rescued_when_the_probes_agree(db_conn):
    """The ordinary case: one card, benchmarked under one model, so the learn row that the
    same sweep wrote can only have come from that model."""
    _learn(db_conn, "NVIDIA A100-SXM4-80GB", 41, 33)
    _probe(db_conn, "NVIDIA A100-SXM4-80GB", "7b")
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA A100-SXM4-80GB|7b"}
    row = db_conn.execute("SELECT batch FROM video_batch_learn WHERE gpu_id=?",
                          ("NVIDIA A100-SXM4-80GB|7b",)).fetchone()
    assert row[0] == 33, "the measured batch must survive the re-key unchanged"


def test_the_regime_tag_is_preserved_in_the_right_position(db_conn):
    """The key is gpu|model|regime, so a compiled row must become `card|7b|c`, not
    `card|c|7b`. Reading it back is what the sizer does, and a transposed key finds nothing."""
    _learn(db_conn, "NVIDIA GeForce RTX 5090|c", 41, 13)
    _probe(db_conn, "NVIDIA GeForce RTX 5090", "7b|c")
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA GeForce RTX 5090|7b|c"}


def test_ambiguous_evidence_leaves_the_row_alone(db_conn):
    """A card benchmarked under TWO models before the split cannot be attributed. Leaving it
    orphaned costs a predictive seed and an OOM back-off; guessing can cost a real OOM."""
    _learn(db_conn, "NVIDIA A100 80GB PCIe", 41, 21)
    _probe(db_conn, "NVIDIA A100 80GB PCIe", "7b")
    _probe(db_conn, "NVIDIA A100 80GB PCIe", "3b", batch=9)
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA A100 80GB PCIe"}, "an ambiguous row must not be guessed"


def test_no_evidence_leaves_the_row_alone(db_conn):
    """A card with learned rows but no probes (a real run tuned it, no benchmark ever ran)
    has nothing to attribute it with."""
    _learn(db_conn, "NVIDIA H100 PCIe", 41, 17)
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA H100 PCIe"}


def test_an_fp8_install_is_migrated_to_fp8_not_to_the_popular_answer(db_conn):
    """The case that rules out a blanket "assume 7B FP16". A 16 GB local card makes the
    wizard write FP8-mixed into video.dit_model, so this install's remote rows are FP8, and
    its own probes say so."""
    _learn(db_conn, "NVIDIA L40S", 41, 9)
    _probe(db_conn, "NVIDIA L40S", "7b_fp8")
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA L40S|7b_fp8"}


def test_already_tagged_rows_are_untouched(db_conn):
    """LOCAL keys were always model-qualified. Re-tagging one would produce `card|7b|7b`."""
    _learn(db_conn, "NVIDIA GeForce RTX 3090|7b|c", 41, 13)
    _learn(db_conn, "NVIDIA GeForce RTX 3090|3b_fp16|c", 41, 25)
    _probe(db_conn, "NVIDIA GeForce RTX 3090", "7b|c")
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA GeForce RTX 3090|7b|c",
                              "NVIDIA GeForce RTX 3090|3b_fp16|c"}


def test_a_real_measurement_is_never_overwritten_by_a_migrated_one(db_conn):
    """If the user has already re-benchmarked the card under the new key, that row is a real
    measurement and outranks anything inferred. The orphan is left where it is rather than
    clobbering it."""
    _learn(db_conn, "NVIDIA GeForce RTX 5090|c", 41, 13)          # old, inferred
    _learn(db_conn, "NVIDIA GeForce RTX 5090|7b|c", 41, 29)       # new, measured
    _probe(db_conn, "NVIDIA GeForce RTX 5090", "7b|c")
    db._migrate_learn_keys_add_model(db_conn)
    row = db_conn.execute("SELECT batch FROM video_batch_learn WHERE gpu_id=?",
                          ("NVIDIA GeForce RTX 5090|7b|c",)).fetchone()
    assert row[0] == 29, "the measured row must win"


def test_the_migration_is_idempotent(db_conn):
    """It runs on every get_conn, so a second pass must find nothing to do and must not
    re-tag what it already tagged."""
    _learn(db_conn, "NVIDIA A100-SXM4-80GB", 41, 33)
    _probe(db_conn, "NVIDIA A100-SXM4-80GB", "7b")
    db._migrate_learn_keys_add_model(db_conn)
    before = _keys(db_conn)
    db._migrate_learn_keys_add_model(db_conn)
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == before == {"NVIDIA A100-SXM4-80GB|7b"}


def test_esrgan_probes_are_not_evidence_about_a_seedvr2_model(db_conn):
    """A Real-ESRGAN benchmark of a card says nothing about which SeedVR2 DiT its learned
    batch came from, and its bench model key ('esrgan-compact') is not a DiT tag at all."""
    _learn(db_conn, "NVIDIA RTX 2000 Ada Generation", 41, 5)
    _probe(db_conn, "NVIDIA RTX 2000 Ada Generation", "esrgan-quality")
    db._migrate_learn_keys_add_model(db_conn)
    assert _keys(db_conn) == {"NVIDIA RTX 2000 Ada Generation"}


def test_the_migrated_key_is_the_one_the_sizer_will_read(db_conn):
    """The end-to-end point of the exercise: after migration, the key a remote run builds
    must find the rescued row. Derived the way the runner derives it, not spelled out.

    The REGIME half still has to match, and that is not a detail this migration can paper
    over: a row measured uncompiled keeps its uncompiled key, so a compiled run correctly
    does NOT read it (that separation is what tile_tag/compile_tag exist for). Both
    directions are asserted here so a future "helpfully" regime-stripping migration fails."""
    import batch_video_upscale as bv
    import video_vram_sizer as sizer
    gpu = "NVIDIA A100-SXM4-80GB"
    _learn(db_conn, gpu, 41, 33)                       # pre-0.6.3, uncompiled, no model tag
    _probe(db_conn, gpu, "7b")
    db._migrate_learn_keys_add_model(db_conn)

    cfg = {"upscale": {}, "video": {"compile": False}}
    vcfg = bv.resolve_video_cfg(cfg)
    same_regime = f"{gpu}|{sizer.model_tag(vcfg.get('dit_model'))}" + sizer.learn_tag(
        bv._engine_flags(vcfg))
    assert db.get_learned_batch(db_conn, same_regime, 41) == 33

    on = bv.resolve_video_cfg({"upscale": {}, "video": {"compile": True}})
    other_regime = f"{gpu}|{sizer.model_tag(on.get('dit_model'))}" + sizer.learn_tag(
        bv._engine_flags(on))
    assert other_regime != same_regime
    assert db.get_learned_batch(db_conn, other_regime, 41) is None,         "a compiled run must not read an uncompiled ceiling"
