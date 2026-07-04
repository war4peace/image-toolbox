"""
The trusted-volume cold-start guard (recommendations item 11): pod/worker.py
seeds the SeedVR2 validation cache from the weight files' size+mtime so the
engine's pre-load check hits instead of re-hashing 16 GB on a cache miss. The
pure builder `_validation_cache_entries` is tested here off the pod (worker.py's
module imports are stdlib-only; the SeedVR2 engine is imported lazily in main()).
"""

from pod import worker


def _touch(path, data=b"x"):
    with open(path, "wb") as f:
        f.write(data)


def test_entries_capture_size_mtime_and_hash(tmp_path):
    dit = tmp_path / "dit.safetensors"
    vae = tmp_path / "vae.safetensors"
    _touch(dit, b"0123456789")
    _touch(vae, b"abc")
    hashes = {"dit.safetensors": "HASH_DIT", "vae.safetensors": "HASH_VAE"}
    entries = worker._validation_cache_entries(
        str(tmp_path), ("dit.safetensors", "vae.safetensors"), hashes.get)

    assert set(entries) == {"dit.safetensors", "vae.safetensors"}
    assert entries["dit.safetensors"]["size"] == 10
    assert entries["dit.safetensors"]["hash"] == "HASH_DIT"
    # mtime matches the file exactly, so the engine's <2s tolerance check hits.
    assert entries["dit.safetensors"]["mtime"] == (tmp_path / "dit.safetensors").stat().st_mtime


def test_missing_and_empty_names_are_skipped(tmp_path):
    _touch(tmp_path / "dit.safetensors")
    entries = worker._validation_cache_entries(
        str(tmp_path), ("dit.safetensors", "absent.safetensors", "", None),
        lambda _n: None)
    assert set(entries) == {"dit.safetensors"}       # only the present, named file
    assert entries["dit.safetensors"]["hash"] is None  # hash_lookup miss -> None


def test_seeded_entry_matches_engine_cache_check(tmp_path):
    # Cross-check against the SAME size+mtime freshness rule the engine uses
    # (downloads.is_file_validated_cached): a cache entry built by our helper must
    # be considered "still valid" for the file it describes.
    dit = tmp_path / "dit.safetensors"
    _touch(dit, b"payload-bytes")
    entries = worker._validation_cache_entries(
        str(tmp_path), ("dit.safetensors",), lambda _n: "H")
    cached = entries["dit.safetensors"]
    st = dit.stat()
    assert cached["size"] == st.st_size
    assert abs(cached["mtime"] - st.st_mtime) < 2
