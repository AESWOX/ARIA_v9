from aria import paths
from aria.routers import logs


def test_logs_router_reads_where_main_writes():
    assert logs._LOG_DIR == paths.log_dir()