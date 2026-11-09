import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run tests marked `live`, which call the real model API and cost money.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="calls the real model API; run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
