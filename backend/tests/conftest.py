"""测试共享固件：每次测试前重建干净的 store 数据，隔离种子数据污染。"""
from __future__ import annotations

import importlib

import pytest

import app.seed as _seed
from app.store import store as shared_store


@pytest.fixture(autouse=True)
def clean_store():
    """每个测试用例使用独立种子数据。

    直接重置已被各 service 单例引用的 store._tables，而不是只 reload
    app.store 模块；否则 from app.store import store 会继续持有旧仓库。
    """
    importlib.reload(_seed)
    shared_store._tables = {
        name: [dict(row) for row in rows]
        for name, rows in _seed.SEED_ROWS.items()
    }
    yield shared_store
