"""
Document processing connector for MIRA
Provides PDF, Word, Excel, and conversion tools
"""
import os
from pathlib import Path
from ..tools import register_tool
# SANDBOX — all document I/O is restricted to this directory.
_SANDBOX = (Path(__file__).parent.parent.parent / "SANDBOX").resolve()


def _sandbox_path(p: str) -> Path:
    """
    Resolve *p* to an absolute path within SANDBOX.
    Accepts bare names ("out.pdf"), SANDBOX-prefixed paths ("SANDBOX/out.pdf"),
    absolute paths already inside SANDBOX, and properly-nested absolute paths.
    Raises ValueError for any path that would escape the SANDBOX tree.
    """
    candidate = Path(p)
    if candidate.is_absolute():
        # Absolute path: resolve directly and check containment.
        # Do NOT do any string replace — it would corrupt mid-path SANDBOX segments.
        resolved = candidate.resolve()
    else:
        # Relative path: strip a leading 'SANDBOX/' or 'SANDBOX\\' prefix only
        # (so agents that write "SANDBOX/out.xlsx" don't double-nest into SANDBOX/SANDBOX/).
        stripped = p.lstrip('/\\')
        if stripped.upper().startswith('SANDBOX/'):
            stripped = stripped[len('SANDBOX/'):]
        elif stripped.upper().startswith('SANDBOX\\'):
            stripped = stripped[len('SANDBOX\\'):]
        resolved = (_SANDBOX / stripped).resolve()
    try:
        resolved.relative_to(_SANDBOX)
    except ValueError:
        raise ValueError(
            f"Security: path '{p}' resolves outside SANDBOX. "
            "All document read/write operations must target files inside SANDBOX/."
        )
    return resolved
# PDF imports
try:
    from PyPDF2 import PdfReader, PdfWriter, PdfMerger
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("[DOCUMENT] PyPDF2 not available - PDF tools disabled")

# Word/DOCX imports
try:
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    print("[DOCUMENT] python-docx not available - Word tools disabled")

# Excel imports
try:
    from openpyxl import Workbook, load_workbook
    import pandas as pd
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False
    print("[DOCUMENT] openpyxl/pandas not available - Excel tools disabled")

# PDF creation imports
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception as e:
    WEASYPRINT_AVAILABLE = False
    WEASYPRINT_ERROR = f"WeasyPrint error: {str(e)}. This usually requires the GTK+ runtime to be installed on Windows. IT CANNOT BE FIXED BY PIP INSTALL."
    print(f"[DOCUMENT] {WEASYPRINT_ERROR}")
    print("[DOCUMENT] Install GTK+ runtime manually on the system or use reportlab instead")

# Reportlab fallback for PDF creation
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    print("[DOCUMENT] reportlab not available - PDF creation limited")

# Markdown imports
try:
    import markdown
    MARKDOWN_AVAILABLE = True
except ImportError:
    MARKDOWN_AVAILABLE = False
    MARKDOWN_ERROR = "Markdown library not installed. IT CANNOT BE AUTOMATICALLY FIXED IN THIS ENVIRONMENT."
    print(f"[DOCUMENT] {MARKDOWN_ERROR}")

# BeautifulSoup imports
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    print("[DOCUMENT] beautifulsoup4 not available - HTML fallback limited")


# ============================================================================
# PDF Tools
# ============================================================================

@register_tool(
    "pdf.read",
    category="Documents",
    description="Extract text from a PDF file",
    example='{"tool": "pdf.read", "args": {"file_path": "report.pdf"}}'
)
def read_pdf(file_path: str = None, filename: str = None, path: str = None, file: str = None) -> dict:
    """
    Extract text from a PDF file.
    
    Args:
        file_path: Path to the PDF file (also accepted as filename / path / file)
    
    Returns:
        Dict with text content and metadata
    """
    file_path = file_path or filename or path or file
    if not file_path:
        return {"status": "error", "error": "file_path is required"}
    if not PDF_AVAILABLE:
        return {"status": "error", "error": "PyPDF2 not installed. Run: pip install PyPDF2"}
    
    try:
        path = Path(file_path)
        if not path.exists():
            return {"status": "error", "error": f"File not found: {file_path}"}
        
        reader = PdfReader(str(path))
        
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n\n"
        
        print(f"[PDF] Extracted text from {len(reader.pages)} pages")
        
        return {
            "text": text.strip(),
            "pages": len(reader.pages),
            "metadata": reader.metadata if reader.metadata else {}
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to read PDF: {str(e)}"}


@register_tool(
    "pdf.create",
    category="Documents",
    description="Create a PDF file from text or HTML content",
    example='{"tool": "pdf.create", "args": {"content": "Hello world", "output_path": "/hello.pdf"}}'
)
def create_pdf(content: str, output_path: str, format: str = "plain") -> dict:
    """
    Create a PDF file from text or HTML content.
    
    Args:
        content: Text or HTML content
        output_path: Path where PDF should be saved
        format: 'plain' for plain text or 'html' for HTML content
    
    Returns:
        Status and path to created PDF
    """
    try:
        output = _sandbox_path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        if format == "html" and WEASYPRINT_AVAILABLE:
            # Use weasyprint for HTML
            HTML(string=content).write_pdf(str(output))
        elif REPORTLAB_AVAILABLE:
            # Fallback to reportlab for plain text
            doc = SimpleDocTemplate(str(output), pagesize=letter)
            styles = getSampleStyleSheet()
            story = []
            
            # Split content into paragraphs
            for para in content.split('\n'):
                if para.strip():
                    p = Paragraph(para, styles['Normal'])
                    story.append(p)
                    story.append(Spacer(1, 0.2 * inch))
            
            doc.build(story)
        else:
            return {
                "status": "error",
                "error": "No PDF library available. Install: pip install reportlab (or weasyprint with GTK+ for HTML support)"
            }
        
        print(f"[PDF] Created PDF at {output}")
        
        return {
            "status": "success",
            "path": str(output),
            "size_bytes": output.stat().st_size
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to create PDF: {str(e)}"}


@register_tool(
    "pdf.merge",
    category="Documents",
    description="Merge multiple PDF files into one",
    example='{"tool": "pdf.merge", "args": {"input_files": ["f1.pdf", "f2.pdf"], "output_path": "merged.pdf"}}'
)
def merge_pdfs(input_files: list, output_path: str) -> dict:
    """
    Merge multiple PDF files into one.
    
    Args:
        input_files: List of PDF file paths to merge
        output_path: Path where merged PDF should be saved
    
    Returns:
        Status and path to merged PDF
    """
    if not PDF_AVAILABLE:
        return {"status": "error", "error": "PyPDF2 not installed. Run: pip install PyPDF2"}
    
    try:
        merger = PdfMerger()
        
        for pdf_path in input_files:
            path = _sandbox_path(pdf_path)
            if not path.exists():
                return {"status": "error", "error": f"File not found in SANDBOX: {pdf_path}"}
            merger.append(str(path))
        
        output = _sandbox_path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        merger.write(str(output))
        merger.close()
        
        print(f"[PDF] Merged {len(input_files)} PDFs into {output}")
        
        return {
            "status": "success",
            "path": str(output),
            "files_merged": len(input_files),
            "size_bytes": output.stat().st_size
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to merge PDFs: {str(e)}"}


@register_tool(
    "pdf.split",
    category="Documents",
    description="Split a PDF into separate files",
    example='{"tool": "pdf.split", "args": {"file_path": "big.pdf", "output_dir": "split_pages/"}}'
)
def split_pdf(file_path: str, output_dir: str, page_ranges: list = None) -> dict:
    """
    Split a PDF into separate files by page or page ranges.
    
    Args:
        file_path: Path to the PDF file to split
        output_dir: Directory where split PDFs should be saved
        page_ranges: Optional list of ranges like [[1,3], [4,5]] or None for individual pages
    
    Returns:
        Status and list of created files
    """
    if not PDF_AVAILABLE:
        return {"status": "error", "error": "PyPDF2 not installed. Run: pip install PyPDF2"}
    
    try:
        path = _sandbox_path(file_path)
        if not path.exists():
            return {"status": "error", "error": f"File not found in SANDBOX: {file_path}"}
        
        reader = PdfReader(str(path))
        output_path_obj = _sandbox_path(output_dir)
        output_path_obj.mkdir(parents=True, exist_ok=True)
        
        created_files = []
        
        if page_ranges:
            # Split by ranges
            for i, (start, end) in enumerate(page_ranges):
                writer = PdfWriter()
                for page_num in range(start - 1, end):  # Convert to 0-indexed
                    if page_num < len(reader.pages):
                        writer.add_page(reader.pages[page_num])
                
                output_file = output_path_obj / f"{path.stem}_pages_{start}-{end}.pdf"
                with open(output_file, "wb") as f:
                    writer.write(f)
                created_files.append(str(output_file))
        else:
            # Split into individual pages
            for page_num, page in enumerate(reader.pages):
                writer = PdfWriter()
                writer.add_page(page)
                
                output_file = output_path_obj / f"{path.stem}_page_{page_num + 1}.pdf"
                with open(output_file, "wb") as f:
                    writer.write(f)
                created_files.append(str(output_file))
        
        print(f"[PDF] Split PDF into {len(created_files)} files")
        
        return {
            "status": "success",
            "files_created": created_files,
            "count": len(created_files)
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to split PDF: {str(e)}"}


# ============================================================================
# Word/DOCX Tools
# ============================================================================

@register_tool(
    "word.create",
    category="Documents",
    description="Create a formatted Word document",
    example='{"tool": "word.create", "args": {"content": {"title": "Doc", "sections": [{"type": "paragraph", "text": "Hello"}]}, "output_path": "doc.docx"}}'
)
def create_word(content: dict, output_path: str) -> dict:
    """
    Create a Word document with formatted content.
    
    Args:
        content: Dict with structure like:
            {
                "title": "Document Title",
                "sections": [
                    {"type": "heading", "text": "Section 1", "level": 1},
                    {"type": "paragraph", "text": "Body text..."},
                    {"type": "bullet_list", "items": ["Item 1", "Item 2"]},
                "]
            }
        output_path: Path where Word document should be saved
    
    Returns:
        Status and path to created document
    """
    if not DOCX_AVAILABLE:
        return {"status": "error", "error": "python-docx not installed. Run: pip install python-docx"}
    
    try:
        doc = Document()
        output = _sandbox_path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        # Add title if provided
        if "title" in content:
            title = doc.add_heading(content["title"], level=0)
            title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        
        # Process sections
        for section in content.get("sections", []):
            section_type = section.get("type", "paragraph")
            
            if section_type == "heading":
                level = section.get("level", 1)
                doc.add_heading(section["text"], level=level)
                
            elif section_type == "paragraph":
                para = doc.add_paragraph(section["text"])
                if section.get("bold"):
                    para.runs[0].bold = True
                if section.get("italic"):
                    para.runs[0].italic = True
                    
            elif section_type == "bullet_list":
                for item in section.get("items", []):
                    doc.add_paragraph(item, style='List Bullet')
                    
            elif section_type == "numbered_list":
                for item in section.get("items", []):
                    doc.add_paragraph(item, style='List Number')
        
        doc.save(str(output))
        
        print(f"[WORD] Created document at {output}")
        
        return {
            "status": "success",
            "path": str(output),
            "size_bytes": output.stat().st_size
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to create Word document: {str(e)}"}


@register_tool(
    "word.read",
    category="Documents",
    description="Extract text from a Word document",
    example='{"tool": "word.read", "args": {"file_path": "doc.docx"}}'
)
def read_word(file_path: str) -> dict:
    """
    Extract text from a Word document.
    
    Args:
        file_path: Path to the Word document
    
    Returns:
        Dict with text content
    """
    if not DOCX_AVAILABLE:
        return {"status": "error", "error": "python-docx not installed. Run: pip install python-docx"}
    
    try:
        path = _sandbox_path(file_path)
        if not path.exists():
            return {"status": "error", "error": f"File not found in SANDBOX: {file_path}"}
        
        doc = Document(str(path))
        
        text = ""
        for para in doc.paragraphs:
            text += para.text + "\n"
        
        print(f"[WORD] Extracted text from {len(doc.paragraphs)} paragraphs")
        
        return {
            "text": text.strip(),
            "paragraphs": len(doc.paragraphs)
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to read Word document: {str(e)}"}


# ============================================================================
# Excel/Spreadsheet Tools
# ============================================================================

@register_tool(
    "excel.create",
    category="Documents",
    description="Create an Excel file from data",
    example='{"tool": "excel.create", "args": {"data": [{"name": "A", "val": 1}], "output_path": "data.xlsx"}}',
    notes='output_path must be a bare filename like "data.xlsx" — do NOT use absolute or relative directory paths'
)
def create_excel(data: list, output_path: str, sheet_name: str = "Sheet1") -> dict:
    """
    Create an Excel file from data.
    
    Args:
        data: List of dicts where keys are column names, e.g.:
              [{"name": "John", "age": 30}, {"name": "Jane", "age": 25}]
        output_path: Path where Excel file should be saved
        sheet_name: Name of the worksheet
    
    Returns:
        Status and path to created file
    """
    if not EXCEL_AVAILABLE:
        return {"status": "error", "error": "openpyxl/pandas not installed. Run: pip install openpyxl pandas"}
    
    try:
        output = _sandbox_path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        df = pd.DataFrame(data)
        df.to_excel(str(output), sheet_name=sheet_name, index=False)
        
        print(f"[EXCEL] Created Excel file with {len(data)} rows at {output}")
        
        return {
            "status": "success",
            "path": str(output),
            "rows": len(data),
            "columns": len(df.columns.tolist()),
            "size_bytes": output.stat().st_size
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to create Excel file: {str(e)}"}


@register_tool(
    "excel.read",
    category="Documents",
    description="Read data from an Excel file",
    example='{"tool": "excel.read", "args": {"file_path": "data.xlsx"}}',
    notes='file_path must be a bare filename like "data.xlsx" — do NOT use absolute or relative directory paths'
)
def read_excel(file_path: str, sheet_name: str = None) -> dict:
    """
    Read data from an Excel file.
    
    Args:
        file_path: Path to the Excel file
        sheet_name: Name of sheet to read (None for first sheet)
    
    Returns:
        Dict with data as list of dicts
    """
    if not EXCEL_AVAILABLE:
        return {"status": "error", "error": "openpyxl/pandas not installed. Run: pip install openpyxl pandas"}
    
    try:
        path = _sandbox_path(file_path)
        if not path.exists():
            return {"status": "error", "error": f"File not found in SANDBOX: {file_path}"}
        
        df = pd.read_excel(str(path), sheet_name=sheet_name or 0)
        data = df.to_dict('records')
        
        print(f"[EXCEL] Read {len(data)} rows from {path}")
        
        return {
            "data": data,
            "rows": len(data),
            "columns": df.columns.tolist()
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to read Excel file: {str(e)}"}


@register_tool(
    "excel.append",
    category="Documents",
    description="Append rows to an existing Excel file",
    example='{"tool": "excel.append", "args": {"file_path": "data.xlsx", "data": [{"name": "B", "val": 2}]}}',
    notes='file_path must be a bare filename like "data.xlsx" — do NOT use absolute or relative directory paths'
)
def append_excel(file_path: str, data: list) -> dict:
    """
    Append rows to an existing Excel file.
    
    Args:
        file_path: Path to the Excel file
        data: List of dicts to append
    
    Returns:
        Status and updated row count
    """
    if not EXCEL_AVAILABLE:
        return {"status": "error", "error": "openpyxl/pandas not installed. Run: pip install openpyxl pandas"}
    
    try:
        path = _sandbox_path(file_path)
        if not path.exists():
            return {"status": "error", "error": f"File not found in SANDBOX: {file_path}"}
        
        # Read existing data
        df_existing = pd.read_excel(str(path))
        
        # Append new data
        df_new = pd.DataFrame(data)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        
        # Save back
        df_combined.to_excel(str(path), index=False)
        
        print(f"[EXCEL] Appended {len(data)} rows to {path}")
        
        return {
            "status": "success",
            "rows_added": len(data),
            "total_rows": len(df_combined)
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to append to Excel file: {str(e)}"}


# ============================================================================
# Document Conversion Tools
# ============================================================================

@register_tool(
    "convert.markdown_to_html",
    category="Conversion",
    description="Convert Markdown text to HTML",
    example='{"tool": "convert.markdown_to_html", "args": {"markdown_content": "# Hello"}}'
)
def markdown_to_html(markdown_content: str = None, output_path: str = None, input: str = None) -> dict:
    """
    Convert Markdown to HTML.
    
    Args:
        markdown_content: Markdown text
        output_path: Optional path to save HTML file
        input: Alias for markdown_content (accepted for compatibility)
    
    Returns:
        HTML content or saved file path
    """
    markdown_content = markdown_content or input
    if not markdown_content:
        return {"status": "error", "error": "markdown_content is required"}
    if not MARKDOWN_AVAILABLE:
        return {"status": "error", "error": "markdown not installed. Run: pip install markdown"}
    
    try:
        html = markdown.markdown(markdown_content, extensions=['extra', 'codehilite'])
        
        if output_path:
            output = _sandbox_path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(html, encoding='utf-8')
            
            print(f"[CONVERT] Saved HTML to {output}")
            
            return {
                "status": "success",
                "path": str(output),
                "html": html
            }
        else:
            return {
                "status": "success",
                "html": html
            }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to convert Markdown to HTML: {str(e)}"}


@register_tool(
    "convert.markdown_to_pdf",
    category="Conversion",
    description="Convert Markdown text to PDF and save in SANDBOX. Pass the markdown text as 'markdown_content' and optionally 'output_path' for the filename.",
    example='{"tool": "convert.markdown_to_pdf", "args": {"markdown_content": "# Hello\\nThis is content", "output_path": "report.pdf"}}'
)
def markdown_to_pdf(markdown_content: str = None, output_path: str = None, input: str = None, output: str = None) -> dict:
    """
    Convert Markdown to PDF (uses weasyprint if available, fallback to reportlab).
    
    Args:
        markdown_content: Markdown text (also accepted as 'input')
        output_path: Path where PDF should be saved (also accepted as 'output'). Defaults to 'output.pdf'.
        input: Alias for markdown_content
        output: Alias for output_path
    
    Returns:
        Status and path to created PDF
    """
    markdown_content = markdown_content or input
    output_path = output_path or output or "output.pdf"  # default so callers can omit it
    if not markdown_content:
        return {"status": "error", "error": "markdown_content is required"}
    if not MARKDOWN_AVAILABLE:
        return {"status": "error", "error": "Required library 'markdown' is missing."}
    
    try:
        # Convert Markdown to HTML first
        html_content = markdown.markdown(markdown_content, extensions=['extra', 'codehilite'])
        
        # Wrap in minimal style
        full_html = f"<html><body>{html_content}</body></html>"
        
        # Call our HTML to PDF tool which has the fallback logic
        return html_to_pdf(full_html, output_path)
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to convert Markdown to PDF: {str(e)}"}


@register_tool(
    "convert.html_to_pdf",
    category="Conversion",
    description="Convert HTML text to PDF",
    example='{"tool": "convert.html_to_pdf", "args": {"html_content": "<h1>Hello</h1>", "output_path": "h.pdf"}}'
)
def html_to_pdf(html_content: str, output_path: str) -> dict:
    """
    Convert HTML to PDF.
    
    Args:
        html_content: HTML string
        output_path: Path where PDF should be saved
    
    Returns:
        Status and path to created PDF
    """
    # Guard: reject bare URLs passed as html_content
    _stripped = (html_content or "").strip()
    if _stripped.startswith(("http://", "https://", "www.")) and "<" not in _stripped:
        return {
            "status": "error",
            "error": (
                "html_to_pdf received a URL instead of HTML content. "
                "Pass the actual HTML string, not a URL. "
                "Use firecrawl.scrape to fetch the page first, then convert the returned markdown."
            )
        }
    # Try weasyprint first, then reportlab
    try:
        output = _sandbox_path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        if WEASYPRINT_AVAILABLE:
            # Use weasyprint for full HTML rendering
            HTML(string=html_content).write_pdf(str(output))
            print(f"[CONVERT] Converted HTML to PDF using weasyprint at {output}")
        elif REPORTLAB_AVAILABLE and BS4_AVAILABLE:
            # Fallback to reportlab (strips HTML tags)
            soup = BeautifulSoup(html_content, 'html.parser')
            text = soup.get_text(separator='\n', strip=True)
            
            doc = SimpleDocTemplate(str(output), pagesize=letter)
            styles = getSampleStyleSheet()
            story = []
            
            # Split content into paragraphs
            for para in text.split('\n'):
                if para.strip():
                    p = Paragraph(para, styles['Normal'])
                    story.append(p)
                    story.append(Spacer(1, 0.2 * inch))
            
            doc.build(story)
            print(f"[CONVERT] Converted HTML to PDF using reportlab (text-only) at {output}")
        elif REPORTLAB_AVAILABLE:
            # Last resort: reportlab without BeautifulSoup (just plain text)
            print("[CONVERT] bs4 missing, converting HTML to PDF as raw text")
            doc = SimpleDocTemplate(str(output), pagesize=letter)
            styles = getSampleStyleSheet()
            story = [Paragraph(html_content, styles['Normal'])]
            doc.build(story)
        else:
            return {
                "status": "error",
                "error": "No PDF library available. Install: pip install reportlab"
            }
        
        return {
            "status": "success",
            "path": str(output),
            "size_bytes": output.stat().st_size
        }
        
    except Exception as e:
        return {"status": "error", "error": f"Failed to convert HTML to PDF: {str(e)}"}
