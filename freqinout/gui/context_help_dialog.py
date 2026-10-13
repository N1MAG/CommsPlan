from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from freqinout.gui.help_registry import HelpContext, get_help_context, resolve_help_host
from freqinout.gui.theme import button_style, resolve_theme


class ContextHelpDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._doc_path = Path(__file__).resolve().parents[2] / "docs" / "guide.html"
        self._current_context = get_help_context(None)

        self.setModal(False)
        self.setWindowTitle("How To")
        self.resize(980, 760)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.title_label = QLabel("<h3>CommsPlan Help</h3>")
        header.addWidget(self.title_label)
        header.addStretch()
        self.open_full_guide_btn = QPushButton("Open Full Guide")
        self.open_full_guide_btn.clicked.connect(self._open_full_guide)
        header.addWidget(self.open_full_guide_btn)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(True)
        layout.addWidget(self.viewer, 1)

        self.apply_theme()

    def apply_theme(self) -> None:
        theme = resolve_theme(self.settings)
        self.open_full_guide_btn.setStyleSheet(button_style("secondary", theme))
        self.close_btn.setStyleSheet(button_style("muted", theme))
        self.summary_label.setStyleSheet(f"color: {theme.get('text_muted', theme.get('text', '#666666'))};")

    def show_help_for(self, context_key: str | None) -> None:
        context = get_help_context(context_key)
        self._current_context = context
        self.title_label.setText(f"<h3>{context.title}</h3>")
        self.summary_label.setText(context.summary or "Review the focused guide section for this part of CommsPlan.")
        self._show_anchor(context.anchor)
        self.show()
        self.raise_()
        self.activateWindow()

    def _show_anchor(self, anchor: str) -> None:
        if not self._doc_path.exists():
            self.viewer.setHtml("<p><b>guide.html was not found.</b></p>")
            return
        base = QUrl.fromLocalFile(str(self._doc_path))
        anchor_txt = str(anchor or "").strip().lstrip("#")
        if anchor_txt:
            self.viewer.setSource(QUrl(f"{base.toString()}#{anchor_txt}"))
        else:
            self.viewer.setSource(base)

    def _open_full_guide(self) -> None:
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_help_anchor"):
            try:
                host.open_help_anchor(self._current_context.anchor, title=self._current_context.title)
                return
            except Exception:
                pass
        self._show_anchor("help")
