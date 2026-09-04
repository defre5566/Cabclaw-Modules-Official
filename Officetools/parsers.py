"""Officetools 解析器：扩展名分发 + 各格式解析 + markdown 产物生成。

支持矩阵（方案 v3 §五）：
- 核心层：.pdf（pdfplumber）/ .docx（python-docx）/ .xlsx（openpyxl）/ .pptx（python-pptx）/ .xls（xlrd）
- OCR 层：.pdf 扫描件（pypdfium2 渲染 → RapidOCR）/ 图片（RapidOCR 直读）
- 旧格式：.doc（antiword）/ .ppt（catppt），系统工具检测到才启用

rc 语义归 worker：本模块抛 ParseError(kind, message)，worker 据此定 rc=0/rc=3。
仅标准库 + pylibs 依赖；不依赖 common。
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CORE_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".xls"}
DOC_TOOL_EXTS = {".doc": "antiword", ".ppt": "catppt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
RANGE_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv",
    ".mp3", ".wav", ".amr", ".aac", ".m4a", ".flac", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".apk",
}

# 扫描件判定：全文档文本层字符数低于该阈值
SCANNED_CHAR_THRESHOLD = 64
# 单页文本层低于该值且后续无改善 → 混合扫描件逐页 OCR 由 _pdf_parse 处理
OCR_RENDER_DPI = 150
OCR_RENDER_MAX_PIXELS = 2200  # 渲染最长边像素上限（防超大页面内存爆炸）

MAX_CELL_CHARS = 200     # 单元格截断
MAX_CELL_COLS = 30       # 表格列上限
MAX_CELL_ROWS = 200      # 表格行上限（xlsx 单表）


class ParseError(Exception):
    """解析失败。kind: encrypted | corrupted | ocr_unavailable | tool_missing。

    worker 按 kind 决定 rc：tool_missing/ocr_unavailable → rc=0 指引；
    encrypted/corrupted → rc=3 转 agent。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class ParseResult:
    md: str                      # 全文 markdown 产物内容
    kind_label: str              # 概览：类型描述（如 "PDF 文档"、"Excel 表格"）
    units_label: str             # 概览：单元描述（如 "12 页"、"3 个工作表"）
    total_chars: int = 0         # 提取正文总字数
    titles: list[str] = field(default_factory=list)   # 标题结构（前若干条）
    excerpt: str = ""            # 开头摘录（正文前 ~300 字）
    truncated: list[str] = field(default_factory=list)  # 截断说明（如 "OCR 仅前 20 页"）
    ocr_pages: int = 0           # OCR 页数（0=非 OCR 路径）


# ---------- markdown 工具 ----------

def _md_escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _md_table(rows: list[list[str]]) -> str:
    """rows（首行为表头）→ markdown 表格；列/行/单元格三重上限防爆炸。"""
    if not rows:
        return ""
    rows = rows[: MAX_CELL_ROWS + 1]
    width = min(max(len(r) for r in rows), MAX_CELL_COLS)
    head = rows[0] + [""] * (width - len(rows[0]))
    lines = [
        "| " + " | ".join(_md_escape_cell(c)[:MAX_CELL_CHARS] for c in head[:width]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in rows[1:]:
        row = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(_md_escape_cell(c)[:MAX_CELL_CHARS] for c in row[:width]) + " |")
    return "\n".join(lines)


def _excerpt(text: str, limit: int = 300) -> str:
    text = text.strip().replace("\r", "")
    return text[:limit]


# ---------- docx ----------

def _parse_docx(path: Path, result: ParseResult) -> None:
    import docx  # noqa: F401  确认依赖后 python-docx API

    document = docx.Document(str(path))
    parts: list[str] = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if style.startswith("heading"):
            try:
                level = int(style.replace("heading", "").strip() or "1")
            except ValueError:
                level = 1
            level = min(max(level, 1), 6)
            parts.append(f"{'#' * level} {text}")
            if len(result.titles) < 30:
                result.titles.append(text)
        else:
            parts.append(text)
    for table in document.tables:
        rows = [[cell.text or "" for cell in row.cells] for row in table.rows]
        t = _md_table(rows)
        if t:
            parts.append(t)
    result.md = "\n\n".join(parts)
    result.kind_label = "Word 文档"
    result.units_label = f"{len(document.paragraphs)} 段"
    result.total_chars = sum(len(p) for p in parts)


# ---------- xlsx ----------

def _parse_xlsx(path: Path, result: ParseResult) -> None:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    sheet_chars = 0
    sheet_names = wb.sheetnames
    for ws in wb.worksheets:
        rows: list[list[str]] = []
        count = 0
        truncated_sheet = False
        for row in ws.iter_rows(values_only=True):
            if count >= MAX_CELL_ROWS:
                truncated_sheet = True
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                rows.append(cells)
                count += 1
        parts.append(f"## 工作表：{ws.title}\n\n" + (_md_table(rows) or "（空表）"))
        if truncated_sheet:
            result.truncated.append(f"工作表「{ws.title}」超过 {MAX_CELL_ROWS} 行已截断")
        sheet_chars += sum(len(c) for r in rows for c in r)
        if ws.title != sheet_names[0] and len(result.titles) < 30:
            result.titles.append(ws.title)
    wb.close()
    result.md = "\n\n".join(parts)
    result.kind_label = "Excel 表格"
    result.units_label = f"{len(sheet_names)} 个工作表"
    result.total_chars = sheet_chars


# ---------- pptx ----------

def _parse_pptx(path: Path, result: ParseResult) -> None:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[str] = []
    chars = 0
    for idx, slide in enumerate(prs.slides, 1):
        title = ""
        body: list[str] = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip() or para.text.strip()
                if not text:
                    continue
                if shape == slide.shapes.title:
                    title = text
                else:
                    body.append(text)
        parts.append(f"## 第 {idx} 页" + (f"：{title}" if title else ""))
        if title and len(result.titles) < 30:
            result.titles.append(f"P{idx} {title}")
        parts.extend(body)
        chars += len(title) + sum(len(b) for b in body)
    result.md = "\n\n".join(parts)
    result.kind_label = "PPT 演示文稿"
    result.units_label = f"{len(prs.slides)} 页"
    result.total_chars = chars


# ---------- xls（旧格式，xlrd） ----------

def _parse_xls(path: Path, result: ParseResult) -> None:
    import xlrd

    book = xlrd.open_workbook(str(path))
    parts: list[str] = []
    chars = 0
    for ws in book.sheets():
        rows: list[list[str]] = []
        count = 0
        for r in range(ws.nrows):
            if count >= MAX_CELL_ROWS:
                result.truncated.append(f"工作表「{ws.name}」超过 {MAX_CELL_ROWS} 行已截断")
                break
            cells = [str(ws.cell_value(r, c)) for c in range(ws.ncols)]
            if any(c.strip() for c in cells):
                rows.append(cells)
                count += 1
        parts.append(f"## 工作表：{ws.name}\n\n" + (_md_table(rows) or "（空表）"))
        chars += sum(len(c) for row in rows for c in row)
        if len(result.titles) < 30:
            result.titles.append(ws.name)
    result.md = "\n\n".join(parts)
    result.kind_label = "Excel 表格（旧版 .xls）"
    result.units_label = f"{book.nsheets} 个工作表"
    result.total_chars = chars


# ---------- pdf ----------

def _pdf_page_text(page) -> str:
    text = page.extract_text() or ""
    tables_md: list[str] = []
    try:
        for table in page.extract_tables():
            t = _md_table([[c or "" for c in row] for row in table])
            if t:
                tables_md.append(t)
    except Exception:
        pass  # 表格提取失败不阻塞文本
    if tables_md:
        text = text + "\n\n" + "\n\n".join(tables_md) if text else "\n\n".join(tables_md)
    return text


def _pdf_needs_ocr(path: Path, max_pages: int) -> tuple[bool, int]:
    """判定扫描件：前 2 页文本层字符总和低于阈值 → 扫描件。返回 (需要OCR, 总页数)。"""
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        sample = "".join((pdf.pages[i].extract_text() or "") for i in range(min(2, total)))
    return len(sample.strip()) < SCANNED_CHAR_THRESHOLD, total


def _pdf_parse_digital(path: Path, result: ParseResult, max_pages: int) -> None:
    import pdfplumber

    parts: list[str] = []
    chars = 0
    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        pages = pdf.pages[:max_pages]
        for idx, page in enumerate(pages, 1):
            text = _pdf_page_text(page).strip()
            parts.append(f"## 第 {idx} 页\n\n{text}" if text else f"## 第 {idx} 页\n\n（无文本层）")
            chars += len(text)
            # 大字号行 → 标题启发式
            if len(result.titles) < 30:
                try:
                    big = [
                        w.get("text", "")
                        for w in (page.extract_words(extra_attrs=["size"]) or [])
                        if w.get("size", 0) >= 16
                    ]
                    line = " ".join(big).strip()
                    if line and len(line) <= 60:
                        result.titles.append(f"P{idx} {line}")
                except Exception:
                    pass
        if total > max_pages:
            result.truncated.append(f"全文 {total} 页，仅解析前 {max_pages} 页")
    result.md = "\n\n".join(parts)
    result.kind_label = "PDF 文档"
    result.units_label = f"{total} 页"
    result.total_chars = chars


def _pdf_parse_ocr(path: Path, result: ParseResult, max_pages: int, ocr_max_pages: int) -> None:
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    engine = RapidOCR()
    doc = pdfium.PdfDocument(str(path))
    total = len(doc)
    pages = min(total, max_pages, ocr_max_pages)
    if total > pages:
        result.truncated.append(f"全文 {total} 页，OCR 仅处理前 {pages} 页")
    parts: list[str] = []
    chars = 0
    try:
        for idx in range(pages):
            page = doc[idx]
            bitmap = page.render(scale=OCR_RENDER_DPI / 72)
            pil_image = bitmap.to_pil()
            if max(pil_image.size) > OCR_RENDER_MAX_PIXELS:
                ratio = OCR_RENDER_MAX_PIXELS / max(pil_image.size)
                pil_image = pil_image.resize(
                    (int(pil_image.width * ratio), int(pil_image.height * ratio))
                )
            import numpy as np

            arr = np.array(pil_image.convert("RGB"))
            ocr_result = engine(arr)
            lines: list[str] = []
            if ocr_result is not None:
                texts = ocr_result.txts if hasattr(ocr_result, "txts") else None
                if texts:
                    lines = list(texts)
            body = "\n".join(lines)
            parts.append(f"## 第 {idx + 1} 页\n\n{body}" if body else f"## 第 {idx + 1} 页\n\n（未识别出文字）")
            chars += len(body)
            result.ocr_pages = idx + 1
    finally:
        doc.close()
    result.md = "\n\n".join(parts)
    result.kind_label = "PDF 文档（扫描件 OCR）"
    result.units_label = f"{total} 页"
    result.total_chars = chars


# ---------- 图片（OCR 层） ----------

def _parse_image(path: Path, result: ParseResult) -> None:
    from rapidocr import RapidOCR

    engine = RapidOCR()
    ocr_result = engine(str(path))
    lines: list[str] = []
    if ocr_result is not None:
        texts = ocr_result.txts if hasattr(ocr_result, "txts") else None
        if texts:
            lines = list(texts)
    body = "\n".join(lines)
    result.md = body or "（未识别出文字）"
    result.kind_label = "图片 OCR"
    result.units_label = "1 张"
    result.total_chars = len(body)


# ---------- 旧格式（系统工具） ----------

def _parse_doc_tool(path: Path, result: ParseResult, tool: str) -> None:
    """antiword（.doc）/ catppt（.ppt）：提取纯文本。"""
    try:
        proc = subprocess.run(
            [tool, str(path)], capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        raise ParseError("corrupted", f"{tool} 处理超时")
    if proc.returncode != 0:
        raise ParseError("corrupted", f"{tool} 失败: {(proc.stderr or '')[:200]}")
    text = (proc.stdout or "").strip()
    if not text:
        raise ParseError("corrupted", f"{tool} 未提取到文字")
    result.md = text
    result.kind_label = "Word 文档（旧版 .doc）" if path.suffix.lower() == ".doc" else "PPT 演示文稿（旧版 .ppt）"
    result.units_label = f"{len(text.splitlines())} 行"
    result.total_chars = len(text)


# ---------- 分发入口 ----------

def supported_label() -> str:
    return "pdf / docx / xlsx / xls / pptx / ppt / doc" + " / 图片(jpg png webp)"


def tool_available(ext: str) -> bool:
    tool = DOC_TOOL_EXTS.get(ext.lower())
    return bool(tool and shutil.which(tool))


def parse(path: Path, settings: dict, ocr_ready: bool) -> ParseResult:
    """按扩展名分发解析。ParseError.kind 见类注释。"""
    ext = path.suffix.lower()
    result = ParseResult(md="", kind_label="", units_label="")

    if ext == ".pdf":
        try:
            needs_ocr, total = _pdf_needs_ocr(path, int(settings.get("max_pages", 300)))
        except Exception as e:
            msg = str(e).lower()
            if "password" in msg or "encrypt" in msg:
                raise ParseError("encrypted", "PDF 已加密") from e
            raise ParseError("corrupted", f"PDF 打开失败: {e}") from e
        if needs_ocr:
            if not ocr_ready:
                raise ParseError("ocr_unavailable", "扫描件需要 OCR 层（web 设置开启后几分钟内自动就绪）")
            _pdf_parse_ocr(path, result, int(settings.get("max_pages", 300)),
                           int(settings.get("ocr_max_pages", 20)))
        else:
            try:
                _pdf_parse_digital(path, result, int(settings.get("max_pages", 300)))
            except Exception as e:
                msg = str(e).lower()
                if "password" in msg or "encrypt" in msg:
                    raise ParseError("encrypted", "PDF 已加密") from e
                raise ParseError("corrupted", f"PDF 解析失败: {e}") from e
        result.excerpt = _excerpt(result.md)
        return result

    if ext == ".docx":
        try:
            _parse_docx(path, result)
        except Exception as e:
            raise ParseError("corrupted", f"docx 解析失败: {e}") from e
    elif ext == ".xlsx":
        try:
            _parse_xlsx(path, result)
        except Exception as e:
            raise ParseError("corrupted", f"xlsx 解析失败: {e}") from e
    elif ext == ".pptx":
        try:
            _parse_pptx(path, result)
        except Exception as e:
            raise ParseError("corrupted", f"pptx 解析失败: {e}") from e
    elif ext == ".xls":
        try:
            _parse_xls(path, result)
        except Exception as e:
            raise ParseError("corrupted", f"xls 解析失败: {e}") from e
    elif ext == ".doc" or ext == ".ppt":
        tool = DOC_TOOL_EXTS[ext]
        if not shutil.which(tool):
            raise ParseError("tool_missing", f"旧格式 {ext} 需要 {tool}（部署机执行 apt install antiword catdoc 后可用）")
        _parse_doc_tool(path, result, tool)
    elif ext in IMAGE_EXTS:
        if not ocr_ready:
            raise ParseError("ocr_unavailable", "图片识别需要 OCR 层（web 设置开启后几分钟内自动就绪）")
        try:
            _parse_image(path, result)
        except Exception as e:
            raise ParseError("corrupted", f"图片 OCR 失败: {e}") from e
    else:
        raise ParseError("unsupported", f"暂不支持 {ext or '无扩展名'} 文件，支持：{supported_label()}")

    result.excerpt = _excerpt(result.md)
    return result
