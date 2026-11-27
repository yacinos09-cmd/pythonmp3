"""Widgets réutilisables : listes réordonnables avec glisser-déposer."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem

ROLE_ID = Qt.ItemDataRole.UserRole

ITEM_FLAGS = (Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
              | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
# Pas de ItemIsDropEnabled sur les éléments : empêche l'imbrication lors d'un déplacement.


def format_duration(seconds: float) -> str:
    s = int(round(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class ReorderTree(QTreeWidget):
    """Liste plate réordonnable par glisser-déposer interne, qui accepte aussi :

    * des fichiers/dossiers venant de l'explorateur (``filesDropped``) ;
    * des éléments venant d'une autre ReorderTree (``foreignDropped``).
    """

    orderChanged = Signal()
    filesDropped = Signal(list, int)      # chemins, ligne cible (-1 = fin)
    foreignDropped = Signal(object, int)  # widget source, ligne cible

    def __init__(self, parent=None, accept_foreign: bool = False):
        super().__init__(parent)
        self.accept_foreign = accept_foreign
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    # -- utilitaires ----------------------------------------------------------
    def ids(self) -> list[str]:
        return [self.topLevelItem(i).data(0, ROLE_ID) for i in range(self.topLevelItemCount())]

    def selected_rows(self) -> list[int]:
        return sorted(self.indexOfTopLevelItem(i) for i in self.selectedItems())

    def item_by_id(self, item_id: str) -> QTreeWidgetItem | None:
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.data(0, ROLE_ID) == item_id:
                return it
        return None

    def _row_at(self, event, between: bool) -> int:
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            return -1
        row = self.indexOfTopLevelItem(item)
        if between and pos.y() > self.visualItemRect(item).center().y():
            row += 1
        return row

    def _is_foreign(self, event) -> bool:
        src = event.source()
        return self.accept_foreign and isinstance(src, ReorderTree) and src is not self

    # -- glisser-déposer ------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or self._is_foreign(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or self._is_foreign(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() and event.source() is None:
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            row = self._row_at(event, between=not self.accept_foreign)
            event.acceptProposedAction()
            if paths:
                QTimer.singleShot(0, lambda: self.filesDropped.emit(paths, row))
            return
        if self._is_foreign(event):
            src, row = event.source(), self._row_at(event, between=False)
            # CopyAction : la vue source ne supprime pas ses éléments, c'est le
            # modèle qui fait foi et qui repeuplera les deux listes.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            QTimer.singleShot(0, lambda: self.foreignDropped.emit(src, row))
            return
        if event.source() is self:
            super().dropEvent(event)
            QTimer.singleShot(0, self.orderChanged.emit)
            return
        event.ignore()
