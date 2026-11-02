"""Draggable AI chat ball, edge tab and movable floating panel."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QParallelAnimationGroup,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)


class ChatBall(QToolButton):
    activated = Signal()
    dropped = Signal()

    def __init__(self, parent: QWidget, size: int = 58):
        super().__init__(parent)
        self.setObjectName("ChatBall")
        self.setText("AI")
        self.setToolTip("打开 AI 问答；拖到右侧边缘可吸附为卡片")
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._press_global: QPoint | None = None
        self._press_pos = QPoint()
        self._dragged = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_pos = self.pos()
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_global is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if delta.manhattanLength() > 4:
            self._dragged = True
        target = self._press_pos + delta
        parent = self.parentWidget()
        if parent is not None:
            max_x = max(0, parent.width() - self.width())
            max_y = max(0, parent.height() - self.height())
            target.setX(max(0, min(target.x(), max_x)))
            target.setY(max(0, min(target.y(), max_y)))
        self.move(target)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._press_global is not None:
            was_drag = self._dragged
            self._press_global = None
            self._dragged = False
            if not was_drag and event.button() == Qt.MouseButton.LeftButton:
                self.activated.emit()
            elif was_drag:
                self.dropped.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PanelDragHandle(QToolButton):
    """Small visible grip for moving the floating chat window."""

    def __init__(self, header: QWidget, panel: QWidget):
        super().__init__(header)
        self.panel = panel
        self.setObjectName("ChatDrag")
        self.setText("⋮⋮")
        self.setToolTip("拖动移动 AI 问答窗口")
        self.setFixedSize(24, 28)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._press: QPoint | None = None
        self._origin = QPoint()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._origin = self.panel.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None:
            super().mouseMoveEvent(event)
            return
        parent = self.panel.parentWidget()
        delta = event.globalPosition().toPoint() - self._press
        target = self._origin + delta
        if parent is not None:
            max_x = max(0, parent.width() - self.panel.width())
            max_y = max(0, parent.height() - self.panel.height())
            target.setX(max(0, min(target.x(), max_x)))
            target.setY(max(0, min(target.y(), max_y)))
        self.panel.move(target)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press = None
        event.accept()


class FloatingChat(QObject):
    """Owns the ball, its right-edge card and the floating chat panel."""

    def __init__(self, parent: QWidget, panel: QFrame, panel_size: tuple[int, int] = (430, 620)):
        super().__init__(parent)
        self.parent_widget = parent
        self.panel = panel
        self.panel_size = panel_size
        self.is_open = False
        self._docked = False
        self.ball = ChatBall(parent)
        self.ball.activated.connect(self.open_panel)
        self.ball.dropped.connect(self._on_ball_dropped)
        self.edge_card = QToolButton(parent)
        self.edge_card.setObjectName("ChatEdge")
        self.edge_card.setText("AI")
        self.edge_card.setToolTip("展开 AI 问答")
        self.edge_card.setFixedSize(26, 64)
        self.edge_card.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edge_card.clicked.connect(self._undock_edge)
        self.edge_card.hide()
        self.panel.setParent(parent)
        self.panel.hide()
        self.panel.setGraphicsEffect(QGraphicsDropShadowEffect(self.panel))
        shadow = self.panel.graphicsEffect()
        if isinstance(shadow, QGraphicsDropShadowEffect):
            shadow.setBlurRadius(26)
            shadow.setOffset(0, 8)
            shadow.setColor(Qt.GlobalColor.black)
            shadow.setColor(shadow.color().darker(220))
        self._opacity = QGraphicsOpacityEffect(self.panel)
        self._opacity.setOpacity(1.0)
        # Geometry animation is enough visually and avoids the extra opacity
        # effect conflicting with the drop shadow.
        self._panel_geom = QPropertyAnimation(self.panel, b"geometry", self)
        self._panel_geom.setDuration(220)
        self._panel_geom.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._group = QParallelAnimationGroup(self)
        self._group.addAnimation(self._panel_geom)
        self._group.finished.connect(self._on_animation_finished)
        self._pending_action = ""
        self._drag_origin: QPoint | None = None
        self._panel_origin = QPoint()
        self._header = panel.findChild(QFrame, "PanelHeader")
        if self._header is not None:
            self._header.installEventFilter(self)
            for label in self._header.findChildren(QLabel):
                label.installEventFilter(self)
            self.drag_handle = PanelDragHandle(self._header, panel)
            layout = self._header.layout()
            if layout is not None:
                layout.insertWidget(1, self.drag_handle)
        self.sync()

    # --------------------------------------------------------------- geometry
    def sync(self) -> None:
        parent = self.parent_widget
        if self.is_open:
            # Once the user has moved the panel, resizing the window must not
            # snap it back to the default location.
            if not self.panel.isVisible() or self.panel.geometry().width() < 100:
                self.panel.setGeometry(self._target_rect())
        elif self._docked:
            self._move_edge_card()
        else:
            if not self.ball.isVisible():
                self.ball.move(self._initial_ball_pos())
            self._clamp_ball()
        self._clamp_panel()

    def _initial_ball_pos(self) -> QPoint:
        parent = self.parent_widget
        x = max(18, parent.width() - self.ball.width() - 36)
        y = max(90, parent.height() - self.ball.height() - 80)
        return QPoint(x, y)

    def _target_rect(self) -> QRect:
        parent = self.parent_widget
        w = min(self.panel_size[0], max(320, parent.width() - 44))
        h = min(self.panel_size[1], max(360, parent.height() - 52))
        x = max(18, parent.width() - w - 26)
        y = max(18, min(74, parent.height() - h - 18))
        return QRect(x, y, w, h)

    def _clamp_ball(self) -> None:
        parent = self.parent_widget
        max_x = max(0, parent.width() - self.ball.width())
        max_y = max(0, parent.height() - self.ball.height())
        pos = self.ball.pos()
        self.ball.move(max(0, min(pos.x(), max_x)), max(0, min(pos.y(), max_y)))

    def _clamp_panel(self) -> None:
        if not self.panel.isVisible():
            return
        parent = self.parent_widget
        max_x = max(0, parent.width() - self.panel.width())
        max_y = max(0, parent.height() - self.panel.height())
        pos = self.panel.pos()
        self.panel.move(max(0, min(pos.x(), max_x)), max(0, min(pos.y(), max_y)))

    # --------------------------------------------------------------- edge tab
    def _on_ball_dropped(self) -> None:
        parent = self.parent_widget
        if self.ball.x() + self.ball.width() >= parent.width() - 24:
            self._dock_edge()

    def _dock_edge(self) -> None:
        if self.is_open:
            return
        self._docked = True
        self.ball.hide()
        self._move_edge_card()
        self.edge_card.show()
        self.edge_card.raise_()

    def _move_edge_card(self) -> None:
        parent = self.parent_widget
        y = max(24, min(self.ball.y(), parent.height() - self.edge_card.height() - 24))
        self.edge_card.move(max(0, parent.width() - self.edge_card.width()), y)

    def _undock_edge(self) -> None:
        if not self._docked:
            return
        self._docked = False
        self.edge_card.hide()
        parent = self.parent_widget
        self.ball.move(
            max(0, parent.width() - self.ball.width() - 24),
            max(24, min(self.edge_card.y(), parent.height() - self.ball.height() - 24)),
        )
        self.ball.show()
        self.ball.raise_()
        self.open_panel()

    # ------------------------------------------------------------------ panel
    def open_panel(self) -> None:
        if self.is_open:
            self.panel.raise_()
            return
        self.is_open = True
        self.edge_card.hide()
        # Use the visible anchor (ball or edge card) as animation origin.
        anchor = self.edge_card if self._docked else self.ball
        start = QRect(
            anchor.x() + anchor.width() // 2 - 12,
            anchor.y() + anchor.height() // 2 - 12,
            24,
            24,
        )
        self.ball.hide()
        self.edge_card.hide()
        self.panel.setGeometry(start)
        self.panel.show()
        self.panel.raise_()
        self._panel_geom.setStartValue(start)
        self._panel_geom.setEndValue(self._target_rect())
        self._pending_action = "open"
        self._group.start()

    def close_panel(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        anchor = self.edge_card if self._docked else self.ball
        self._panel_geom.setStartValue(self.panel.geometry())
        self._panel_geom.setEndValue(
            QRect(
                anchor.x() + anchor.width() // 2 - 12,
                anchor.y() + anchor.height() // 2 - 12,
                24,
                24,
            )
        )
        self._pending_action = "close"
        self._group.start()

    def _on_animation_finished(self) -> None:
        action = self._pending_action
        self._pending_action = ""
        if action == "close":
            self.panel.hide()
            if self._docked:
                self._move_edge_card()
                self.edge_card.show()
                self.edge_card.raise_()
            else:
                self.ball.show()
                self.ball.raise_()
        elif action == "open":
            self.panel.raise_()

    # ------------------------------------------------------------ panel drag
    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is not self._header and not isinstance(obj, QLabel):
            return super().eventFilter(obj, event)
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return super().eventFilter(obj, event)
            global_pos = event.globalPosition().toPoint()
            child = self._header.childAt(self._header.mapFromGlobal(global_pos))
            if isinstance(child, (QPushButton, QToolButton, QComboBox)):
                return False
            self._drag_origin = event.globalPosition().toPoint()
            self._panel_origin = self.panel.pos()
            return True
        if event.type() == QEvent.Type.MouseMove and self._drag_origin is not None:
            delta = event.globalPosition().toPoint() - self._drag_origin
            self.panel.move(self._panel_origin + delta)
            self._clamp_panel()
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and self._drag_origin is not None:
            self._drag_origin = None
            return True
        return super().eventFilter(obj, event)
