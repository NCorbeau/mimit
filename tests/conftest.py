from pathlib import Path

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in Path(str(item.path)).parts:
            item.add_marker(pytest.mark.integration)
