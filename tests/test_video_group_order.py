"""
Pure-logic tests for the (engine, gpu) queue grouping (docs/video-upscaler.md section 18):
`batch_video_upscale.group_queue_order` must make same-(engine, gpu) jobs contiguous (one pod
session each), preserve each job's order WITHIN its group (stable), and never touch the DB.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import batch_video_upscale as bv


def _job(rel, engine=None, gpu=None):
    """A dict shaped like the columns group_queue_order reads (engine/gpu/rel)."""
    return {"rel_path": rel, "engine": engine, "gpu": gpu, "target": "4K", "clip_id": 0}


def _rels(jobs):
    return [j["rel_path"] for j in jobs]


def test_groups_are_contiguous():
    # Interleaved by GPU on input; must come out grouped by (engine, gpu).
    jobs = [
        _job("a", "seedvr2", "PRO6000"),
        _job("b", "fixed_ratio", "RTX2000"),
        _job("c", "seedvr2", "PRO6000"),
        _job("d", "fixed_ratio", "RTX2000"),
        _job("e", "seedvr2", "PRO4000"),
    ]
    out = bv.group_queue_order(jobs)
    keys = [bv.job_group_key(j) for j in out]
    # Each group key appears as one unbroken run.
    seen = []
    for k in keys:
        if not seen or seen[-1] != k:
            seen.append(k)
    assert len(seen) == len(set(seen)), f"a group was split: {keys}"


def test_within_group_order_preserved():
    jobs = [
        _job("a", "seedvr2", "PRO6000"),
        _job("b", "seedvr2", "PRO6000"),
        _job("c", "seedvr2", "PRO6000"),
    ]
    # Single group: order is untouched.
    assert _rels(bv.group_queue_order(jobs)) == ["a", "b", "c"]


def test_first_appearance_default_order():
    # No rank given: groups follow first appearance (a's group first, then b's).
    jobs = [
        _job("a", "seedvr2", "PRO6000"),
        _job("b", "fixed_ratio", "RTX2000"),
        _job("c", "seedvr2", "PRO6000"),
    ]
    assert _rels(bv.group_queue_order(jobs)) == ["a", "c", "b"]


def test_group_rank_orders_between_groups():
    jobs = [
        _job("a", "seedvr2", "PRO6000"),
        _job("b", "fixed_ratio", "RTX2000"),
    ]
    # Cheapest first: rank the ESRGAN/RTX2000 group ahead of the SeedVR2/PRO6000 group.
    price = {("fixed_ratio", "RTX2000"): 0.24, ("seedvr2", "PRO6000"): 1.99}
    out = bv.group_queue_order(jobs, group_rank=lambda k: price[k])
    assert _rels(out) == ["b", "a"]


def test_legacy_nulls_group_together():
    # NULL engine -> seedvr2, NULL gpu -> '' : legacy rows form one group, unchanged order.
    jobs = [_job("a"), _job("b"), _job("c")]
    assert _rels(bv.group_queue_order(jobs)) == ["a", "b", "c"]
    assert all(bv.job_group_key(j) == ("seedvr2", "") for j in jobs)


def test_rank_tie_keeps_groups_contiguous():
    # Two distinct groups with the SAME rank must still each stay contiguous.
    jobs = [
        _job("a", "seedvr2", "G1"),
        _job("b", "seedvr2", "G2"),
        _job("c", "seedvr2", "G1"),
        _job("d", "seedvr2", "G2"),
    ]
    out = bv.group_queue_order(jobs, group_rank=lambda k: 0)   # all tie
    keys = [bv.job_group_key(j) for j in out]
    runs = []
    for k in keys:
        if not runs or runs[-1] != k:
            runs.append(k)
    assert len(runs) == len(set(runs)), f"a tied group was split: {keys}"


def test_input_not_mutated():
    jobs = [_job("a", "seedvr2", "G2"), _job("b", "seedvr2", "G1")]
    before = _rels(jobs)
    bv.group_queue_order(jobs)
    assert _rels(jobs) == before   # returns a new list; input order intact
