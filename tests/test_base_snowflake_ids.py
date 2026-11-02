"""雪花 ID 静态护栏用例：模型列 / 契约字段 / 白名单 / 增量 / 自检与缺规则分支。"""

from __future__ import annotations

import io
import sys
import tomllib
from pathlib import Path

import pytest
from base_fixtures import config, write

from engkit.base import snowflake_ids
from engkit.config import parse

MODEL_OK = "    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True)\n"
MODEL_BAD = "    tenant_id: Mapped[int | None] = mapped_column(\n        BigInteger, nullable=True\n    )\n"
MODEL_COUNT = "    size: Mapped[int] = mapped_column(BigInteger)\n"
SCHEMA_BAD = "    user_id: int\n"
SCHEMA_OK = "    user_id: SnowflakeIdStr\n"


def test_scan_text_model_and_schema(tmp_path: Path) -> None:
    """模型裸 BigInteger / schema int 残留命中；合规样本与计数列不误报。"""
    rule = config(tmp_path).snowflake_ids_rules
    assert rule is not None
    assert snowflake_ids.scan_text("src/models/demo.py", MODEL_OK + MODEL_COUNT, rule) == []
    model_problems = snowflake_ids.scan_text("src/models/demo.py", MODEL_BAD, rule)
    assert len(model_problems) == 1 and "tenant_id" in model_problems[0]
    schema_problems = snowflake_ids.scan_text("src/schemas/demo.py", SCHEMA_BAD, rule)
    assert len(schema_problems) == 1 and "user_id" in schema_problems[0]
    assert snowflake_ids.scan_text("src/schemas/demo.py", SCHEMA_OK, rule) == []


def test_whitelist_and_semantic_names(tmp_path: Path) -> None:
    """白名单按 `路径::列名` 豁免；语义 ID 名（tenant_scope）命中。"""
    rule = config(tmp_path).snowflake_ids_rules
    assert rule is not None
    assert (
        snowflake_ids.scan_text("src/models/demo.py", "    size: Mapped[int] = mapped_column(BigInteger)\n", rule) == []
    )
    problems = snowflake_ids.scan_text("src/models/other.py", MODEL_BAD, rule)
    assert problems
    semantic = "    tenant_scope: Mapped[int] = mapped_column(BigInteger)\n"
    assert snowflake_ids.scan_text("src/models/other.py", semantic, rule)


def test_run_full_incremental_and_missing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量扫描、增量清单过滤（含 Windows 路径与非目标文件）、剪枝与缺文件跳过。"""
    write(
        tmp_path,
        {
            "src/models/demo.py": MODEL_BAD,
            "src/models/clean.py": MODEL_OK,
            "src/tests/ignored.py": MODEL_BAD,
            "other/x.py": MODEL_BAD,
        },
    )
    cfg = config(tmp_path)
    assert snowflake_ids.run(cfg) == 1
    assert "tenant_id" in capsys.readouterr().out

    changed = tmp_path / "changed.txt"
    changed.write_text("src\\models\\clean.py\nother/x.py\n", encoding="utf-8")
    assert snowflake_ids.run(cfg, files_from=str(changed)) == 0

    changed.write_text("src/models/missing.py\n", encoding="utf-8")
    assert snowflake_ids.run(cfg, files_from=str(changed)) == 0

    # 清单来源 `-`：门禁通过管道递清单，反斜杠归一后与文件来源等价
    monkeypatch.setattr(sys, "stdin", io.StringIO(r"src\models\clean.py" + "\nother/x.py\n"))
    assert snowflake_ids.run(cfg, files_from="-") == 0


def test_run_clean_and_self_test(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全量通过输出计数；内置自检通过。"""
    write(tmp_path, {"src/models/clean.py": MODEL_OK})
    cfg = config(tmp_path)
    assert snowflake_ids.run(cfg) == 0
    assert "通过" in capsys.readouterr().out
    assert snowflake_ids.run(cfg, as_self_test=True) == 0


def test_run_without_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """未登记规则返回 2 并提示键位。"""
    raw = tomllib.loads("[docs]\nroot = 'docs'\n")
    cfg = parse(raw, tmp_path / "engkit.toml")
    assert snowflake_ids.run(cfg) == 2
    assert "snowflake_ids" in capsys.readouterr().out
