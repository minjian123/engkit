"""定向档装箱重分布用例（按服务分桶轮转，Kiwi 2544）。"""

from __future__ import annotations

import pytest

from engkit import preflight

pytestmark = pytest.mark.kiwi_id(2544)


def test_service_key_maps_services_libs_and_other() -> None:
    """服务键推导：`services/<名>` / `libs/<名>` / 其余归 other。"""
    assert preflight.service_key("services/platform/tests/api/test_a.py") == "platform"
    assert preflight.service_key("services/notification") == "notification"
    assert preflight.service_key("libs/bms_core/tests/test_x.py") == "libs-bms_core"
    assert preflight.service_key("tests/test_root.py") == "other"
    assert preflight.service_key("services") == "other"
    assert preflight.service_key("") == "other"


def test_bucket_by_service_orders_buckets_by_size() -> None:
    """分桶：桶内保序，桶按字符总量降序（等量按服务键升序）；空输入返回空。"""
    paths = ["libs/a/x.py", "services/msg/tests/test_b.py", "services/msg/tests/test_a.py"]
    buckets = preflight.bucket_by_service(paths)
    assert buckets == [
        ["services/msg/tests/test_b.py", "services/msg/tests/test_a.py"],
        ["libs/a/x.py"],
    ]
    assert preflight.bucket_by_service([]) == []


def test_bucket_by_service_ties_break_on_key() -> None:
    """字符量相同的桶按服务键升序，装箱结果可复现。"""
    paths = ["libs/b/x.py", "libs/a/x.py"]
    assert preflight.bucket_by_service(paths) == [["libs/a/x.py"], ["libs/b/x.py"]]


def test_pack_paths_round_robin_keeps_set_equivalent_and_budget() -> None:
    """轮转装箱：目标集合无重无漏，每批拼接长度不超预算。"""
    targets = [f"services/svc{i % 4}/tests/test_{i}.py" for i in range(24)]
    batches = preflight.pack_paths(targets, 120, round_robin=True)
    flattened = [path for batch in batches for path in batch]
    assert sorted(flattened) == sorted(targets)
    assert len(flattened) == len(set(flattened))
    for batch in batches:
        assert sum(len(path) + 1 for path in batch) <= 120


def test_pack_paths_round_robin_spreads_services() -> None:
    """服务分散：同一服务至少落在两批（写热点不聚堆）。"""
    targets = [f"services/svc{i % 4}/tests/test_{i}.py" for i in range(24)]
    batches = preflight.pack_paths(targets, 120, round_robin=True)
    assert len(batches) >= 2
    for service in ("svc0", "svc1", "svc2", "svc3"):
        holders = [index for index, batch in enumerate(batches) if any(service in path for path in batch)]
        assert len(holders) >= 2


def test_pack_paths_round_robin_is_deterministic_and_handles_empty() -> None:
    """同一输入两次装箱结果一致；空输入返回空批次。"""
    targets = [f"services/svc{i % 3}/tests/test_{i}.py" for i in range(10)]
    assert preflight.pack_paths(targets, 90, round_robin=True) == preflight.pack_paths(targets, 90, round_robin=True)
    assert preflight.pack_paths([], 90, round_robin=True) == []


def test_pack_paths_round_robin_seals_oversized_target() -> None:
    """单个目标自身超预算 ⇒ 独占一批；后续目标不再拼进这一批（封口）。"""
    oversized = "services/big/tests/" + "x" * 20 + ".py"
    small = "services/big/tests/t.py"
    batches = preflight.pack_paths([oversized, small], 25, round_robin=True)
    assert [oversized] in batches
    assert [small] in batches
    holder = next(batch for batch in batches if oversized in batch)
    assert holder == [oversized]


def test_pack_paths_sequential_stays_default() -> None:
    """缺省仍是顺序贪婪（同批内保持输入顺序），轮转需显式开启。"""
    targets = ["a.py", "bb.py", "ccc.py"]
    assert preflight.pack_paths(targets, 6) == [["a.py"], ["bb.py"], ["ccc.py"]]
    assert preflight.pack_paths([], 6) == []


def test_pack_batches_prefers_rotation(monkeypatch) -> None:
    """轮转批数在「顺序 +1」以内 ⇒ 采用轮转；同数不打印碎片化说明。"""

    def fake(paths, budget=preflight.DEFAULT_ARGV_BUDGET, *, round_robin=False):
        return [["a"], ["b"]] if round_robin else [["a", "b"]]

    monkeypatch.setattr(preflight, "pack_paths", fake)
    assert preflight._pack_batches(["a", "b"], 10) == [["a"], ["b"]]


def test_pack_batches_notes_fragmentation_within_limit(monkeypatch, capsys) -> None:
    """轮转比顺序多一批（碎片化 +1，允许上限内）⇒ 采用轮转并说明原因。"""

    def fake(paths, budget=preflight.DEFAULT_ARGV_BUDGET, *, round_robin=False):
        return [["a"], ["b"], ["c"]] if round_robin else [["a", "b"], ["c"]]

    monkeypatch.setattr(preflight, "pack_paths", fake)
    assert preflight._pack_batches(["a", "b", "c"], 10) == [["a"], ["b"], ["c"]]
    assert "碎片化 +1" in capsys.readouterr().out


def test_pack_batches_falls_back_when_rotation_fragments(monkeypatch, capsys) -> None:
    """轮转碎片化超过「顺序 +1」⇒ 回退顺序装箱（批数不增）。"""

    def fake(paths, budget=preflight.DEFAULT_ARGV_BUDGET, *, round_robin=False):
        return [["a"], ["b"], ["c"], ["d"]] if round_robin else [["a", "b", "c", "d"]]

    monkeypatch.setattr(preflight, "pack_paths", fake)
    assert preflight._pack_batches(["a", "b", "c", "d"], 10) == [["a", "b", "c", "d"]]
    assert "回退顺序装箱" in capsys.readouterr().out


def test_pack_batches_empty_input() -> None:
    """空输入 ⇒ 空批次（不新开批）。"""
    assert preflight._pack_batches([], 6000) == []
