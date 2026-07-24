"""Remote Real-ESRGAN (#18 B) wiring: the volume-free esrgan pod shape, the engine-aware
GPU floor, and the grouped-path gate that routes a fixed_ratio group to an esrgan pod.

Pure/offline: RemoteSession.__init__ hits no network (it reads config dicts + local key
files), and the estimate/grouping helpers are pure. The live pod round-trip is validated
separately (a real pod, the user's side)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import video_estimate as ve
import batch_video_upscale as bv
from remote_run import RemoteSession, ESRGAN_IMAGE, DEFAULT_IMAGE


# ── engine-aware VRAM floor ──────────────────────────────────────────────────

def test_fixed_ratio_job_uses_low_vram_floor():
    # A Real-ESRGAN job is light regardless of target: never the SeedVR2 32/80/90 floors.
    for target in ("1080p", "1440p", "4K"):
        assert ve.job_vram_floor({"engine": "fixed_ratio", "target": target}) == \
            ve.ESRGAN_VRAM_FLOOR
    assert ve.ESRGAN_VRAM_FLOOR <= 16          # clears the cheapest deployable cards


def test_seedvr2_job_keeps_its_target_floor():
    assert ve.job_vram_floor({"engine": "seedvr2", "target": "4K"}) == ve.VRAM_FLOOR["4K"]
    # NULL engine defaults to seedvr2.
    assert ve.job_vram_floor({"engine": None, "target": "1080p"}) == ve.VRAM_FLOOR["1080p"]


def test_max_target_floor_is_engine_aware():
    # A fixed_ratio-only queue floors low even at 4K.
    esr = [{"engine": "fixed_ratio", "target": "4K"},
           {"engine": "fixed_ratio", "target": "1080p"}]
    assert ve.max_target_floor(esr) == ve.ESRGAN_VRAM_FLOOR
    # A mixed queue floors at the heaviest SeedVR2 job (the picker still admits by whichever
    # is being configured; this is the whole-queue ceiling).
    mixed = esr + [{"engine": "seedvr2", "target": "4K"}]
    assert ve.max_target_floor(mixed) == ve.VRAM_FLOOR["4K"]


# ── grouped-path gate: a fixed_ratio group must be detected ───────────────────

def _job(rel, target, engine, gpu):
    return {"rel_path": rel, "target": target, "clip_id": 0, "engine": engine, "gpu": gpu}


def test_job_group_key_splits_by_engine_and_gpu():
    a = _job("a.mp4", "1080p", "fixed_ratio", "NVIDIA L4")
    b = _job("b.mp4", "1080p", "seedvr2", "NVIDIA L4")
    assert bv.job_group_key(a) == ("fixed_ratio", "NVIDIA L4")
    assert bv.job_group_key(b) == ("seedvr2", "NVIDIA L4")
    assert bv.job_group_key(a) != bv.job_group_key(b)


def test_lone_fixed_ratio_group_triggers_grouped_gate():
    # A single fixed_ratio group is ONE distinct key, so the old ">1 group" gate would miss
    # it and wrongly take the SeedVR2 single-pod path. The new gate also fires on any
    # fixed_ratio group, so an esrgan-only queue reaches the volume-free esrgan pod.
    jobs = [_job("a.mp4", "1080p", "fixed_ratio", "NVIDIA L4"),
            _job("b.mp4", "720p", "fixed_ratio", "NVIDIA L4")]
    keys = bv.distinct_group_keys(jobs)
    assert len(keys) == 1                                   # single group
    gate = len(keys) > 1 or any(k[0] == "fixed_ratio" for k in keys)
    assert gate is True

    # A single SeedVR2 group must NOT trigger the grouped path (byte-identical legacy path).
    sv = [_job("a.mp4", "4K", "seedvr2", "NVIDIA RTX PRO 6000")]
    kk = bv.distinct_group_keys(sv)
    assert (len(kk) > 1 or any(k[0] == "fixed_ratio" for k in kk)) is False


# ── RemoteSession esrgan pod shape ───────────────────────────────────────────

def _mk(mode):
    return RemoteSession({"api_key": "", "ssh_key_path": ""}, {}, ROOT, mode=mode)


def test_esrgan_session_has_own_pod_name_and_mode():
    s = _mk("esrgan")
    assert s.mode == "esrgan"
    assert s.worker_mode == "esrgan"
    # Its OWN pod name/prefix, so _find_existing_pod never adopts a volume SeedVR2 pod.
    assert s.pod_name == "esrgan-toolbox-remote"
    assert s.pod_name_prefix == "esrgan-toolbox"


def test_video_and_esrgan_pods_do_not_share_a_name():
    assert _mk("video").pod_name != _mk("esrgan").pod_name
    assert _mk("video").pod_name_prefix != _mk("esrgan").pod_name_prefix


def test_esrgan_worker_version_differs_from_video():
    # The hashed file set is mode-aware (esrgan hashes the fixed-ratio stack, not SeedVR2),
    # so a reused pod reloads when the mode's own code changes without cross-triggering.
    assert _mk("esrgan").worker_version != _mk("video").worker_version


def test_esrgan_image_is_a_runpod_low_cuda_image():
    # Must be a runpod/* image (sshd + PUBLIC_KEY) and lower-CUDA than the SeedVR2 default
    # (which fails on a host driver a patch below cu1281). See remote-esrgan-cuda-image memory.
    assert ESRGAN_IMAGE.startswith("runpod/")
    assert "cuda12.4" in ESRGAN_IMAGE
    assert ESRGAN_IMAGE != DEFAULT_IMAGE


def test_cuda_floor_parses_both_tag_forms():
    # The GeForce CUDA floor must resolve the image's CUDA from BOTH the runpod short tag
    # (cu1281) and the official-pytorch long tag (cuda12.4) the esrgan image uses.
    import runpod_client as rp
    assert rp._cuda_from_image(DEFAULT_IMAGE) == "12.8"
    assert rp._cuda_from_image(ESRGAN_IMAGE) == "12.4"
    floor = rp.allowed_cuda_versions(ESRGAN_IMAGE)
    assert floor and "12.4" in floor and "12.8" in floor      # 12.4+ admits the whole fleet
