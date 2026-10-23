from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Tuple

from PySide6.QtCore import QEvent, QSize, Qt, QUrl
from PySide6.QtGui import QFont, QImage, QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QBoxLayout,
    QListWidget,
    QListWidgetItem,
    QTextBrowser,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QPushButton,
    QFileDialog,
    QMessageBox,
    QDialog,
    QPlainTextEdit,
    QApplication,
)

from freqinout.gui.theme import button_height_for_font, control_height_for_font, resolve_theme, button_style, label_style
from freqinout.gui.bounded_snapshot_worker import SnapshotWorkerController
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.logger import get_recent_issues


class HelpTab(QWidget):
    """
    Displays docs/guide.html with a section index for quick navigation.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc_path = Path(__file__).resolve().parents[2] / "docs" / "guide.html"
        self.settings = SettingsManager()
        self._document_generation = 0
        self._document_html = ""
        self._pending_anchor = ""
        self._document_worker: SnapshotWorkerController | None = None

        layout = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._help_layout = layout

        # The topic rail never takes vertical space away from the article.
        self.toc_panel = QWidget()
        self.toc_panel.setObjectName('helpContentsPanel')
        self.toc_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        toc_col = QVBoxLayout(self.toc_panel)
        toc_col.setContentsMargins(0, 0, 0, 0)
        self.toc_title = QLabel("Contents")
        self.toc_title.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        toc_col.addWidget(self.toc_title)
        self.topic_filter = QLineEdit()
        self.topic_filter.setPlaceholderText('Filter topics')
        self.topic_filter.setAccessibleName('Filter guide topics')
        self.topic_filter.setClearButtonEnabled(True)
        self.topic_filter.textChanged.connect(self._filter_topics)
        toc_col.addWidget(self.topic_filter)
        self.toc_list = QListWidget()
        self.toc_list.setAccessibleName('Guide contents')
        self.toc_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.toc_list.setTextElideMode(Qt.ElideRight)
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        self.toc_list.itemActivated.connect(self._on_toc_clicked)
        toc_col.addWidget(self.toc_list, stretch=1)
        self.no_topics = QLabel('No matching topics')
        self.no_topics.setWordWrap(True)
        self.no_topics.hide()
        toc_col.addWidget(self.no_topics)
        layout.addWidget(self.toc_panel)

        # Right: viewer
        self.viewer_panel = QWidget()
        self.viewer_panel.setMinimumWidth(0)
        viewer_col = QVBoxLayout(self.viewer_panel)
        viewer_col.setContentsMargins(0, 0, 0, 0)
        header_row = QHBoxLayout()
        self.guide_title = QLabel("CommsPlan Guide")
        header_row.addWidget(self.guide_title)
        header_row.addStretch()
        self.contents_btn = QPushButton('Contents')
        self.contents_btn.setCheckable(True)
        self.contents_btn.setChecked(True)
        self.contents_btn.setAccessibleName('Show guide contents')
        self.contents_btn.setToolTip('Show or hide the left topic sidebar')
        self.contents_btn.toggled.connect(self.toc_panel.setVisible)
        header_row.addWidget(self.contents_btn)
        viewer_col.addLayout(header_row)
        self.tools_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.export_pdf_btn = QPushButton("Export to PDF")
        theme = resolve_theme(self.settings)
        self.export_pdf_btn.setStyleSheet(button_style("secondary", theme))
        self.export_pdf_btn.clicked.connect(self._export_pdf)
        self.tools_row.addWidget(self.export_pdf_btn)
        self.recent_issues_btn = QPushButton("Recent Issues")
        self.recent_issues_btn.setStyleSheet(button_style("secondary", theme))
        self.recent_issues_btn.clicked.connect(self._show_recent_issues)
        self.tools_row.addWidget(self.recent_issues_btn)
        self.tools_row.addStretch()
        viewer_col.addLayout(self.tools_row)

        search_row = QHBoxLayout()
        self.guide_search = QLineEdit()
        self.guide_search.setPlaceholderText('Find in guide')
        self.guide_search.setAccessibleName('Find text in the guide')
        self.guide_search.setClearButtonEnabled(True)
        self.guide_search.setMinimumWidth(0)
        self.guide_search.returnPressed.connect(self._find_next)
        self.guide_search.textChanged.connect(lambda: self.search_status.clear())
        search_row.addWidget(self.guide_search, stretch=1)
        self.find_previous_btn = QPushButton('Previous')
        self.find_previous_btn.setAccessibleName('Previous guide match')
        self.find_previous_btn.clicked.connect(lambda: self._find_next(backward=True))
        search_row.addWidget(self.find_previous_btn)
        self.find_next_btn = QPushButton('Next')
        self.find_next_btn.setAccessibleName('Next guide match')
        self.find_next_btn.clicked.connect(lambda: self._find_next())
        search_row.addWidget(self.find_next_btn)
        viewer_col.addLayout(search_row)
        self.search_status = QLabel()
        self.search_status.setAccessibleName('Guide search result')
        viewer_col.addWidget(self.search_status)
        self.find_shortcut = QShortcut(QKeySequence.Find, self)
        self.find_shortcut.activated.connect(self._focus_guide_search)
        self.previous_shortcut = QShortcut(QKeySequence('Shift+Return'), self.guide_search)
        self.previous_shortcut.setContext(Qt.WidgetShortcut)
        self.previous_shortcut.activated.connect(lambda: self._find_next(backward=True))

        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(True)
        self.viewer.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.viewer.setHtml("<p>Loading the CommsPlan guide…</p>")
        self.viewer.setMinimumWidth(0)
        viewer_col.addWidget(self.viewer, stretch=1)

        layout.addWidget(self.viewer_panel, stretch=1)
        self._update_responsive_layout()
        self.apply_theme()
        self._request_document_load()
        self.destroyed.connect(self._stop_document_worker)

    def _ensure_document_worker(self) -> SnapshotWorkerController:
        worker = self._document_worker
        if worker is None:
            worker = SnapshotWorkerController(self, self._on_document_ready)
            self._document_worker = worker
        return worker

    def _stop_document_worker(self) -> None:
        worker = getattr(self, "_document_worker", None)
        if worker is not None:
            worker.stop()
            self._document_worker = None

    def shutdown(self) -> None:
        self._stop_document_worker()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual
        self.shutdown()
        super().closeEvent(event)

    @staticmethod
    def _read_document_snapshot(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="ignore")

    @staticmethod
    def _resolve_local_image_urls(html_text: str, document_path: Path) -> str:
        """Give Qt's viewer and PDF printer explicit URLs for local guide images."""

        base_dir = Path(document_path).resolve().parent
        pattern = re.compile(
            r'(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>["\'])(?P<src>.*?)(?P=quote)',
            flags=re.IGNORECASE | re.DOTALL,
        )

        def replace(match: re.Match[str]) -> str:
            source = html.unescape(match.group("src")).strip()
            if not source or source.startswith(("data:", "file:", "http:", "https:", "qrc:", "#")):
                return match.group(0)
            candidate = Path(source)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            candidate = candidate.resolve()
            if not candidate.is_file():
                return match.group(0)
            quote = match.group("quote")
            return f'{match.group("prefix")}{quote}{QUrl.fromLocalFile(str(candidate)).toString()}{quote}'

        return pattern.sub(replace, str(html_text))

    @staticmethod
    def _register_local_image_resources(document: QTextDocument, html_text: str) -> None:
        """Keep local images resident so QTextDocument printing cannot drop them."""

        for source in re.findall(
            r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\']',
            str(html_text),
            flags=re.IGNORECASE | re.DOTALL,
        ):
            url = QUrl(html.unescape(source).strip())
            if not url.isLocalFile():
                continue
            image = QImage(url.toLocalFile())
            if image.isNull():
                continue
            document.addResource(QTextDocument.ImageResource, url, image)

    def _request_document_load(self) -> None:
        self._document_generation += 1
        generation = self._document_generation
        path = Path(self._doc_path)
        self._ensure_document_worker().request(
            generation,
            lambda: HelpTab._read_document_snapshot(path),
        )

    def _on_document_ready(self, generation: int, payload: object, error: object) -> None:
        if int(generation) != self._document_generation:
            return
        if error is not None or not isinstance(payload, str):
            self.viewer.setPlainText(f"The CommsPlan guide could not be loaded: {error or 'invalid document'}")
            return
        try:
            # QTextBrowser.setHtml() accepts only the HTML string in PySide6.
            # Set the document base URL separately so relative guide assets
            # still resolve without aborting the completion callback.
            document = self.viewer.document()
            document.setBaseUrl(QUrl.fromLocalFile(str(self._doc_path)))
            rendered_payload = self._resolve_local_image_urls(payload, self._doc_path)
            self._register_local_image_resources(document, rendered_payload)
            self.viewer.setHtml(rendered_payload)
            self._build_toc(payload)
        except Exception as exc:
            self._document_html = ""
            self.toc_list.clear()
            self.viewer.setPlainText(f"The CommsPlan guide could not be displayed: {exc}")
            return
        self._document_html = payload
        self._update_responsive_layout()
        if self._pending_anchor:
            pending = self._pending_anchor
            self._pending_anchor = ""
            self.open_anchor(pending)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.apply_theme()

    def _update_responsive_layout(self) -> None:
        """Bound the left rail independently of the longest guide heading."""
        if not hasattr(self, "toc_panel"):
            return
        metrics = self.fontMetrics()
        # Content hints must never force the article below its navigation.
        rail_width = min(metrics.averageCharWidth() * 30,
                         max(metrics.averageCharWidth() * 18, self.width() // 3))
        self.toc_panel.setFixedWidth(rail_width)
        for index in range(self.toc_list.count()):
            item = self.toc_list.item(index)
            item.setSizeHint(QSize(0, control_height_for_font(self.toc_list)))
        self.topic_filter.setMinimumHeight(control_height_for_font(self.topic_filter))
        self.guide_search.setMinimumHeight(control_height_for_font(self.guide_search))
        tool_width = sum(button.sizeHint().width() for button in
                         (self.export_pdf_btn, self.recent_issues_btn)) + self.tools_row.spacing()
        self.tools_row.setDirection(QBoxLayout.TopToBottom if self.viewer_panel.width() < tool_width
                                   else QBoxLayout.LeftToRight)

    def _focus_guide_search(self) -> None:
        self.guide_search.setFocus()
        self.guide_search.selectAll()

    def _find_next(self, backward: bool = False) -> None:
        query = self.guide_search.text().strip()
        if not query:
            self.search_status.setText('Enter text to find in the guide.')
            self.guide_search.setFocus()
            return
        flags = QTextDocument.FindBackward if backward else QTextDocument.FindFlags()
        if self.viewer.find(query, flags):
            self.search_status.setText('Match found')
            return
        previous = self.viewer.textCursor()
        cursor = QTextCursor(self.viewer.document())
        cursor.movePosition(QTextCursor.End if backward else QTextCursor.Start)
        self.viewer.setTextCursor(cursor)
        if self.viewer.find(query, flags):
            self.search_status.setText('Wrapped to last match' if backward else 'Wrapped to first match')
        else:
            self.viewer.setTextCursor(previous)
            self.search_status.setText('No matches')

    def _filter_topics(self, query: str) -> None:
        query = query.strip().casefold()
        ancestors = []
        visible = set()
        for index in range(self.toc_list.count()):
            item = self.toc_list.item(index)
            level = int(item.data(Qt.UserRole + 1) or 1)
            while ancestors and ancestors[-1][1] >= level:
                ancestors.pop()
            breadcrumb = ' '.join(self.toc_list.item(parent).text() for parent, _ in ancestors)
            if not query or query in (breadcrumb + ' ' + item.text()).casefold():
                visible.add(index)
                visible.update(parent for parent, _ in ancestors)
            ancestors.append((index, level))
        for index in range(self.toc_list.count()):
            self.toc_list.item(index).setHidden(index not in visible)
        self.no_topics.setVisible(not visible)

    def apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        self.toc_title.setStyleSheet(label_style("text", theme, weight=700))
        self.guide_title.setStyleSheet(label_style("text", theme, weight=700))
        self.export_pdf_btn.setStyleSheet(button_style("secondary", theme))
        self.recent_issues_btn.setStyleSheet(button_style("secondary", theme))
        self.contents_btn.setStyleSheet(button_style("secondary", theme))
        self.no_topics.setStyleSheet(label_style("muted", theme))
        self.search_status.setStyleSheet(label_style("muted", theme))
        for button in (self.find_previous_btn, self.find_next_btn):
            button.setStyleSheet(button_style('secondary', theme))
        for button in (self.export_pdf_btn, self.recent_issues_btn, self.contents_btn,
                       self.find_previous_btn, self.find_next_btn):
            button.setMinimumHeight(button_height_for_font(button))
        levels = [self.toc_list.item(index).data(Qt.UserRole + 1)
                  for index in range(self.toc_list.count())]
        base_level = min((level for level in levels if level), default=1)
        for index, level in enumerate(levels):
            font = QFont(self.toc_list.font())
            font.setBold(level == base_level)
            self.toc_list.item(index).setFont(font)
        self._update_responsive_layout()

    def _show_recent_issues(self) -> None:
        issues = get_recent_issues()
        text = "\n".join(issues) if issues else "No recent warnings or errors have been recorded in this session."
        dialog = QDialog(self)
        dialog.setWindowTitle("Recent Issues")
        dialog.resize(820, 420)
        layout = QVBoxLayout(dialog)
        intro = QLabel("Recent warnings and errors from this CommsPlan session.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text)
        layout.addWidget(box, stretch=1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        copy_btn = QPushButton("Copy")
        close_btn = QPushButton("Close")
        theme = resolve_theme(self.settings)
        copy_btn.setStyleSheet(button_style("secondary", theme))
        close_btn.setStyleSheet(button_style("secondary", theme))
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(box.toPlainText()))
        close_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        dialog.exec()

    def _export_pdf(self) -> None:
        if not self._doc_path.exists():
            QMessageBox.warning(self, "Missing guide", "guide.html was not found.")
            return
        default_path = str(self._doc_path.parent / "CommsPlan User Guide.pdf")
        fn, _ = QFileDialog.getSaveFileName(
            self,
            "Export Guide to PDF",
            default_path,
            "PDF Files (*.pdf)",
        )
        if not fn:
            return
        try:
            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(fn)
            self.viewer.document().print_(printer)
            QMessageBox.information(self, "Export complete", f"Saved PDF to:\n{fn}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", f"PDF export failed:\n{e}")

    def _parse_headings(self, html_text: str) -> List[Tuple[int, str, str]]:
        """
        Return a list of (level, anchor, text) for h1/h2/h3/h4 tags that have ids.
        """
        headings: List[Tuple[int, str, str]] = []
        pattern = re.compile(
            r"<h([1-4])([^>]*)>(.*?)</h\1>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(html_text):
            level = int(match.group(1))
            attrs = match.group(2) or ""
            raw_text = match.group(3)
            anchor = ""
            if attrs:
                id_match = re.search(r'\bid\s*=\s*"([^"]+)"', attrs, flags=re.IGNORECASE)
                if not id_match:
                    id_match = re.search(r"\bid\s*=\s*'([^']+)'", attrs, flags=re.IGNORECASE)
                if id_match:
                    anchor = id_match.group(1)
            # Strip tags and unescape entities
            text_clean = re.sub("<[^>]+>", "", raw_text)
            text_clean = html.unescape(text_clean).strip()
            if text_clean:
                headings.append((level, anchor, text_clean))
        return headings

    def open_anchor(self, anchor: str | None) -> None:
        anchor_txt = str(anchor or "").strip().lstrip("#")
        if not self._document_html:
            self._pending_anchor = anchor_txt
            return
        if anchor_txt:
            self.viewer.scrollToAnchor(anchor_txt)
            self._select_toc_anchor(anchor_txt)
            return
        self.viewer.verticalScrollBar().setValue(0)

    def _select_toc_anchor(self, anchor: str) -> None:
        if not hasattr(self, "toc_list"):
            return
        target = str(anchor or "").strip().lstrip("#")
        if not target:
            return
        for idx in range(self.toc_list.count()):
            item = self.toc_list.item(idx)
            if item is None:
                continue
            stored = str(item.data(Qt.UserRole) or "").strip().lstrip("#")
            if stored == target:
                if item.isHidden():
                    self.topic_filter.clear()
                self.toc_list.setCurrentRow(idx)
                self.toc_list.scrollToItem(item)
                break

    def _build_toc(self, html_text: str | None = None):
        """
        Parse headings from the guide to build a formatted index.
        """
        html_text = self._document_html if html_text is None else str(html_text)
        if not html_text:
            return
        headings = self._parse_headings(html_text)
        self.toc_list.clear()
        if not headings:
            self.toc_list.addItem("(No headings found)")
            return
        # The guide title is already in the reading pane; emphasize sections
        # rather than repeating it above every topic.
        if any(level > 1 for level, _, _ in headings):
            headings = [heading for heading in headings if heading[0] > 1]
        base_level = min(level for level, _, _ in headings)
        for level, anchor, text in headings:
            indent = "    " * (level - base_level)
            item = QListWidgetItem(f"{indent}{text}")
            item.setToolTip(text)
            item.setData(Qt.UserRole + 1, level)
            # Store anchor when available; otherwise store text for find() fallback
            item.setData(Qt.UserRole, anchor or text)
            font = QFont(self.toc_list.font())
            font.setBold(level == base_level)
            item.setFont(font)
            self.toc_list.addItem(item)
        self._filter_topics(self.topic_filter.text())

    def _on_toc_clicked(self, item: QListWidgetItem):
        target = item.data(Qt.UserRole)
        if not target:
            return
        if target.startswith("#"):
            target = target.lstrip("#")
        # Try anchor if we have one
        if target and not target.strip().isspace() and "#" not in target and re.match(r"^[A-Za-z0-9_-]+$", target):
            self.open_anchor(str(target))
        else:
            # Fallback: find text in document
            doc = self.viewer.document()
            cursor = doc.find(str(target))
            if cursor and not cursor.isNull():
                self.viewer.setTextCursor(cursor)
