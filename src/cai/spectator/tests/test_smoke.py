"""冒烟测试：包骨架可导入（T002）。"""

import spectator
from spectator import config, core, evaluator, logging_utils, matcher, models, service  # noqa: F401


def test_package_importable():
    assert spectator is not None
