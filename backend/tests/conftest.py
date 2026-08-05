"""pytest configuration — enables anyio for async test functions."""
import pytest


def pytest_collection_modifyitems(items):
    """Auto-apply anyio marker to all async test functions."""
    for item in items:
        if item.get_closest_marker("anyio") is None:
            # Check if it's an async function
            if hasattr(item, "obj") and item.obj and hasattr(item.obj, "__code__"):
                try:
                    import inspect
                    if inspect.iscoroutinefunction(item.obj):
                        item.add_marker(pytest.mark.anyio)
                except Exception:
                    pass
