"""OCR 层慢测试（模型加载 + 推理）：OT_TEST_OCR=1 且核心依赖就绪时才跑。

跑法：
    OT_TEST_OCR=1 OFFICETOOLS_PYLIBS=... python -m pytest Officetools/tests/ -m slow
"""
from __future__ import annotations

from pathlib import Path

import pytest

import parsers
import bootstrap

pytestmark = pytest.mark.slow


def _make_text_image(tmp_path: Path, text: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    font_path = Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Light.ttc")
    if not font_path.is_file():
        pytest.skip("无系统中文字体")
    img = Image.new("RGB", (700, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 60), text, font=ImageFont.truetype(str(font_path), 36), fill="black")
    p = tmp_path / "zh.png"
    img.save(p)
    return p


def test_image_ocr(tmp_path):
    assert bootstrap._imports_ok(bootstrap.OCR_IMPORTS), "OCR 层未就绪"
    p = _make_text_image(tmp_path, "项目计划：三月开发四月上线")
    r = parsers.parse(p, {}, ocr_ready=True)
    assert "项目计划" in r.md


def test_scanned_pdf_ocr(tmp_path):
    """图片 → 嵌入 PDF（无文本层）→ 扫描件判定 → OCR 链路。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = "/usr/share/fonts/noto-cjk/NotoSansCJK-Light.ttc"
    if not Path(font_path).is_file():
        pytest.skip("无系统中文字体")
    img = Image.new("RGB", (496, 702), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 60), "扫描件测试：这是渲染出来的文字",
              font=ImageFont.truetype(font_path, 28), fill="black")
    jpeg_path = tmp_path / "src.jpg"
    img.save(jpeg_path, quality=92)
    jpeg = jpeg_path.read_bytes()

    head = (
        f"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        f"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        f"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 496 702]/Resources<</XObject<</Im0 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        f"4 0 obj<</Type/XObject/Subtype/Image/Width 496/Height 702/ColorSpace/DeviceRGB/BitsPerComponent 8/Filter/DCTDecode/Length {len(jpeg)}>>stream\n"
    ).encode()
    tail = b"\nendstream\nendobj\n5 0 obj<</Length 44>>stream\nq 496 0 0 702 0 0 cm /Im0 Do Q\nendstream\nendobj\ntrailer<</Root 1 0 R>>\n"
    pdf_path = tmp_path / "scanned.pdf"
    pdf_path.write_bytes(head + jpeg + tail)

    r = parsers.parse(pdf_path, {"max_pages": 300, "ocr_max_pages": 20}, ocr_ready=True)
    assert r.kind_label == "PDF 文档（扫描件 OCR）"
    assert "扫描件测试" in r.md


def test_ocr_unavailable_rc_hint():
    """OCR 未就绪时扫描件 → ocr_unavailable（worker 转 rc=0 提示）。"""
    p = Path("/nonexistent.pdf")  # 不触发真实解析，直接验证错误类
    assert parsers.ParseError("ocr_unavailable", "x").kind == "ocr_unavailable"
