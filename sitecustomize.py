"""Install the offline guard in Python subprocesses spawned by test runs."""
import os

if os.environ.get("LUMIBOT_OFFLINE_GUARD") == "1":
    from strategies.runtime_records import install_test_context
    install_test_context(os.environ.get("LUMIBOT_TEST_RECORD_ROOT"))
