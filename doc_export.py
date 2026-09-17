"""
Word document export for analysis session outputs.

Maintains a living document throughout the session, accumulating
analysis outputs as sections. Users can save individual responses,
ranges, or the entire session to a downloadable .docx file.
"""

import io
import re
from datetime import datetime
from typing import Optional

from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH


def _markdown_to_docx_paragraph(doc: Document, text: str):
    """Convert basic markdown text to docx paragraphs.

    Handles headers (##), bold (**), bullet points (-), tables (|),
    and code blocks (```). Not a full markdown parser — covers the
    patterns the agent actually produces.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Code block
        if line.strip().startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if code_lines:
                p = doc.add_paragraph()
                p.style = doc.styles["No Spacing"]
                run = p.add_run("\n".join(code_lines))
                run.font.name = "Courier New"
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
            i += 1
            continue

        # Headers
        if line.startswith("#### "):
            doc.add_heading(line[5:].strip(), level=4)
        elif line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)

        # Table row
        elif line.strip().startswith("|") and "|" in line.strip()[1:]:
            # Collect all table rows
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row_text = lines[i].strip()
                # Skip separator rows (|---|---|)
                if not re.match(r"^\|[\s\-:|]+\|$", row_text):
                    cells = [c.strip() for c in row_text.split("|")[1:-1]]
                    if cells:
                        table_rows.append(cells)
                i += 1
            if table_rows:
                num_cols = max(len(r) for r in table_rows)
                table = doc.add_table(rows=len(table_rows), cols=num_cols)
                table.style = "Light Grid Accent 1"
                for row_idx, row_data in enumerate(table_rows):
                    for col_idx, cell_text in enumerate(row_data):
                        if col_idx < num_cols:
                            table.rows[row_idx].cells[col_idx].text = cell_text
            continue

        # Bullet points
        elif line.strip().startswith("- ") or line.strip().startswith("* "):
            content = line.strip()[2:]
            p = doc.add_paragraph(style="List Bullet")
            _add_formatted_run(p, content)

        # Numbered list
        elif re.match(r"^\s*\d+\.\s", line.strip()):
            content = re.sub(r"^\s*\d+\.\s", "", line.strip())
            p = doc.add_paragraph(style="List Number")
            _add_formatted_run(p, content)

        # Empty line
        elif not line.strip():
            pass  # Skip blank lines

        # Regular paragraph
        else:
            p = doc.add_paragraph()
            _add_formatted_run(p, line)

        i += 1


def _add_formatted_run(paragraph, text: str):
    """Add text to a paragraph with basic bold/italic formatting."""
    # Split on **bold** patterns
    parts = re.split(r"(\*\*.*?\*\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        else:
            paragraph.add_run(part)


class SessionDocument:
    """Manages a living Word document for the analysis session."""

    def __init__(self):
        self.entries: list[dict] = []
        self._saved_indices: set[int] = set()

    def add_entry(self, question: str, response: str):
        """Record an analysis exchange (question + response)."""
        self.entries.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "question": question,
            "response": response,
        })

    @property
    def total_entries(self) -> int:
        return len(self.entries)

    @property
    def unsaved_count(self) -> int:
        return len(self.entries) - len(self._saved_indices)

    def save_current(self) -> int:
        """Mark the latest entry for export. Returns the entry index."""
        if not self.entries:
            return -1
        idx = len(self.entries) - 1
        self._saved_indices.add(idx)
        return idx

    def save_last_n(self, n: int) -> list[int]:
        """Mark the last N entries for export."""
        indices = []
        start = max(0, len(self.entries) - n)
        for idx in range(start, len(self.entries)):
            self._saved_indices.add(idx)
            indices.append(idx)
        return indices

    def save_range(self, start: int, end: int) -> list[int]:
        """Mark entries from start to end (1-indexed, inclusive) for export."""
        indices = []
        for idx in range(max(0, start - 1), min(len(self.entries), end)):
            self._saved_indices.add(idx)
            indices.append(idx)
        return indices

    def save_all(self) -> list[int]:
        """Mark all entries for export."""
        indices = list(range(len(self.entries)))
        self._saved_indices = set(indices)
        return indices

    def generate_docx(self, title: Optional[str] = None) -> io.BytesIO:
        """Generate a Word document from all saved entries.

        Returns a BytesIO buffer ready for download.
        """
        doc = Document()

        # Title
        heading = doc.add_heading(
            title or "Media Analysis Session Report",
            level=0,
        )
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Metadata
        meta = doc.add_paragraph()
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = meta.add_run(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
            f"Entries: {len(self._saved_indices)} of {len(self.entries)}"
        )
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

        doc.add_paragraph()  # Spacer

        # Sorted by original order
        for idx in sorted(self._saved_indices):
            if idx >= len(self.entries):
                continue
            entry = self.entries[idx]

            # Section header
            doc.add_heading(
                f"Analysis {idx + 1} — {entry['timestamp']}",
                level=1,
            )

            # Question
            q_heading = doc.add_heading("Question", level=2)
            doc.add_paragraph(entry["question"])

            # Response
            doc.add_heading("Response", level=2)
            _markdown_to_docx_paragraph(doc, entry["response"])

            # Page break between entries (except last)
            if idx != max(self._saved_indices):
                doc.add_page_break()

        # Write to buffer
        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)
        return buffer

    def has_unsaved(self) -> bool:
        """Check if there are entries not yet marked for export."""
        return len(self._saved_indices) < len(self.entries)

    def get_save_prompt(self) -> str:
        """Generate the save prompt to append after each analysis response."""
        total = len(self.entries)
        saved = len(self._saved_indices)
        unsaved = total - saved

        lines = ["\n---", "📄 **Session Document Options:**"]

        if unsaved > 0:
            lines.append(
                f"- **Save this output** — Add this response to the session document"
            )
        lines.append(
            "- **Save and finish** — Save and download the document, then clean up files"
        )
        lines.append(
            "- **Continue without saving** — Ask your next question"
        )

        if total > 1:
            lines.append(
                f"- **Save all outputs** — Add all {total} responses to the document"
            )
            if unsaved > 1:
                lines.append(
                    f"- **Save last N** — e.g., \"Save the last 3 responses\""
                )

        if saved > 0:
            lines.append(
                f"\n_📋 Document status: {saved} of {total} response(s) saved_"
            )

        return "\n".join(lines)
