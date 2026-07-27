from pathlib import Path

import pytest

from app.config import Settings
from app.services.ocr_service import OCRService


def service_without_ocr_setup() -> OCRService:
    return OCRService.__new__(OCRService)


def test_markdown_files_are_text_like(tmp_path: Path):
    markdown = tmp_path / "sample.md"
    markdown.write_text("# Opening\n\nA real punchline lives here.\n", encoding="utf-8")

    text = service_without_ocr_setup().extract_text(markdown)

    assert "Opening" in text
    assert "real punchline" in text


def test_plain_text_files_still_extract(tmp_path: Path):
    plain_text = tmp_path / "sample.txt"
    plain_text.write_text("Plain text still works.", encoding="utf-8")

    assert service_without_ocr_setup().extract_text(plain_text) == "Plain text still works."


def test_unsupported_files_still_raise(tmp_path: Path):
    unsupported = tmp_path / "sample.json"
    unsupported.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported file type"):
        service_without_ocr_setup().extract_text(unsupported)


def test_default_settings_allow_markdown_extensions():
    settings = Settings()

    assert "md" in settings.allowed_extensions_list
    assert "markdown" in settings.allowed_extensions_list
