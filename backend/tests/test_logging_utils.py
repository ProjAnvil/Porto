from __future__ import annotations

from porto_chatbot.logging_utils import get_component_logger


def test_component_logger_writes_to_settings_log_dir(sample_settings):
    logger = get_component_logger("unit-test.component", sample_settings)

    logger.info("hello from logging test")
    for handler in logger.handlers:
        handler.flush()

    log_file = sample_settings.log_dir / "unit-test.component.log"
    assert log_file.exists()
    assert "hello from logging test" in log_file.read_text(encoding="utf-8")
