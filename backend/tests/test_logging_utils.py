from __future__ import annotations

import logging as _stdlib_logging

from loguru import logger as _loguru

from porto_chatbot.logging_utils import (
    get_component_logger,
    get_logger,
    setup_logging,
)


def test_component_logger_writes_to_app_log(sample_settings):
    """get_component_logger routes through InterceptHandler -> loguru -> app.log."""
    setup_logging(sample_settings, enqueue=False)

    logger = get_component_logger("unit-test.component", sample_settings)
    logger.info("hello from logging test")

    log_file = sample_settings.log_dir / "app.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "hello from logging test" in content
    # Component tag should appear in the formatted output.
    assert "unit-test.component" in content


def test_percent_style_formatting_works(sample_settings):
    """stdlib %-style args are formatted before reaching loguru."""
    setup_logging(sample_settings, enqueue=False)

    logger = get_component_logger("fmt-test", sample_settings)
    logger.info("workflow_id=%s status=%s", "wf-123", "running")

    content = (sample_settings.log_dir / "app.log").read_text(encoding="utf-8")
    assert "workflow_id=wf-123 status=running" in content


def test_get_logger_loguru_native(sample_settings):
    """get_logger returns a loguru logger with {}-style formatting."""
    setup_logging(sample_settings, enqueue=False)

    log = get_logger("new-module", sample_settings)
    log.info("processed {} items", 42)

    content = (sample_settings.log_dir / "app.log").read_text(encoding="utf-8")
    assert "processed 42 items" in content
    assert "new-module" in content


def test_intercept_handler_captures_third_party(sample_settings):
    """Third-party stdlib loggers are captured via root InterceptHandler."""
    setup_logging(sample_settings, enqueue=False)

    ext_logger = _stdlib_logging.getLogger("some.external.library")
    ext_logger.warning("external lib warning")

    content = (sample_settings.log_dir / "app.log").read_text(encoding="utf-8")
    assert "external lib warning" in content
    assert "some.external.library" in content


def test_enqueue_writes_async(sample_settings):
    """enqueue=True writes asynchronously; complete() flushes the queue."""
    setup_logging(sample_settings, enqueue=True)

    logger = get_component_logger("async-test", sample_settings)
    logger.info("async message")
    _loguru.complete()  # wait for the background queue thread

    content = (sample_settings.log_dir / "app.log").read_text(encoding="utf-8")
    assert "async message" in content


def test_exception_logging(sample_settings):
    """Exception tracebacks are captured via logger.exception()."""
    setup_logging(sample_settings, enqueue=False)

    logger = get_component_logger("exc-test", sample_settings)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("caught error: %s", "detail")

    content = (sample_settings.log_dir / "app.log").read_text(encoding="utf-8")
    assert "ValueError: boom" in content
    assert "caught error: detail" in content


def test_rotation_creates_backup(sample_settings):
    """Log rotation creates backup files when size threshold is exceeded."""
    setup_logging(
        sample_settings,
        enqueue=False,
        level="DEBUG",
        rotation="500 B",
        retention=3,
        compression=None,
    )

    log = get_logger("rotate-test", sample_settings)
    for i in range(100):
        log.info("rotation test line {:04d} padding padding padding", i)
    _loguru.complete()

    rotated = list(sample_settings.log_dir.glob("app*.log"))
    assert len(rotated) > 1, f"Expected rotated backups, got {rotated}"
