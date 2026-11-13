#!/usr/bin/env python
"""隔离模式测试入口：python -I scripts/run_tests.py

-I 不读取用户站点包、环境变量与当前目录隐式导入；本脚本显式把仓库根
加入 sys.path，使 src 包在隔离模式下仍可导入，避免被工作目录中的
同名模块（如伪造的 json.py）劫持。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(1 if result.failures or result.errors else 0)
