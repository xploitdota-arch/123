import math
import random
from pathlib import Path

from PyQt6.QtWidgets import QWidget, QGraphicsDropShadowEffect
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QFontDatabase, QLinearGradient,
    QRadialGradient, QPainterPath, QPen
)


class SplashParticle:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(0.3, 1.5)
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed - random.uniform(0.5, 1.5)
        self.life = random.uniform(0.5, 1.0)
        self.decay = random.uniform(0.008, 0.02)
        self.size = random.uniform(1.5, 4)

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += 0.02
        self.life -= self.decay
        self.size *= 0.995

    def alive(self):
        return self.life > 0 and self.size > 0.3


class SplashScreen(QWidget):
    """Animated splash screen with progress bar and particles."""

    def __init__(self, on_finished=None):
        super().__init__()
        self.on_finished = on_finished

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Amaterasu")  # Для корректного отображения в таскбаре
        self.setFixedSize(480, 300)

        # Center on screen
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen().geometry()
        self.move(
            (screen.width() - self.width()) // 2,
            (screen.height() - self.height()) // 2
        )

        # Load fonts
        font_path = Path(__file__).parent.parent / "fonts" / "yuji-syuku.ttf"
        fid = QFontDatabase.addApplicationFont(str(font_path))
        if fid != -1:
            families = QFontDatabase.applicationFontFamilies(fid)
            self._title_font = families[0] if families else "Segoe UI"
        else:
            self._title_font = "Segoe UI"

        mc_font_path = Path(__file__).parent.parent / "fonts" / "minecraft-rus.ttf"
        fid2 = QFontDatabase.addApplicationFont(str(mc_font_path))
        if fid2 != -1:
            families2 = QFontDatabase.applicationFontFamilies(fid2)
            self._mc_font = families2[0] if families2 else "Segoe UI"
        else:
            self._mc_font = "Segoe UI"

        # State
        self._progress = 0.0       # 0..1
        self._target_progress = 0.0
        self._phase = 0.0
        self._particles: list[SplashParticle] = []
        self._alpha = 255          # for fade out
        self._status_text = "Инициализация..."
        self._finished = False

        # Timers
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)  # ~60fps
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start()

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(40)
        self._progress_timer.timeout.connect(self._advance_progress)
        self._progress_timer.start()

    def _advance_progress(self):
        """Simulate loading progress."""
        if self._finished:
            return

        if self._target_progress < 0.25:
            self._target_progress += random.uniform(0.01, 0.04)
            self._status_text = "Загрузка ресурсов..."
        elif self._target_progress < 0.50:
            self._target_progress += random.uniform(0.008, 0.03)
            self._status_text = "Подготовка интерфейса..."
        elif self._target_progress < 0.75:
            self._target_progress += random.uniform(0.01, 0.035)
            self._status_text = "Проверка версий..."
        elif self._target_progress < 0.95:
            self._target_progress += random.uniform(0.015, 0.04)
            self._status_text = "Почти готово..."
        else:
            self._target_progress = 1.0
            self._status_text = "Готово!"
            self._progress_timer.stop()
            # Start fade out after a short delay
            QTimer.singleShot(400, self._start_fade_out)

    def _start_fade_out(self):
        self._finished = True

    def _tick(self):
        self._phase += 0.05

        # Smooth progress interpolation — плавнее
        diff = self._target_progress - self._progress
        self._progress += diff * 0.05

        # Spawn particles along progress bar
        if self._progress > 0.01 and not self._finished:
            bar_x = 40 + self._progress * 400
            bar_y = 220
            for _ in range(2):
                self._particles.append(SplashParticle(
                    bar_x + random.randint(-5, 5),
                    bar_y + random.randint(-8, 8)
                ))

        # Update particles
        for p in self._particles:
            p.update()
        self._particles = [p for p in self._particles if p.alive()]

        # Fade out
        if self._finished:
            self._alpha = max(0, self._alpha - 3)  # плавнее исчезновение ~1.4сек
            if self._alpha <= 0:
                self._anim_timer.stop()
                self.hide()
                if self.on_finished:
                    self.on_finished()
                return

        self.update()

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._alpha / 255.0)

        # ─── Background with rounded corners ───
        bg_path = QPainterPath()
        bg_path.addRoundedRect(QRectF(0, 0, w, h), 16, 16)
        painter.setClipPath(bg_path)

        # Dark gradient background
        bg_grad = QLinearGradient(0, 0, 0, h)
        bg_grad.setColorAt(0, QColor(18, 8, 32))
        bg_grad.setColorAt(0.5, QColor(10, 4, 20))
        bg_grad.setColorAt(1, QColor(15, 6, 28))
        painter.fillRect(QRectF(0, 0, w, h), bg_grad)

        # ─── Subtle vignette ───
        vig = QRadialGradient(w / 2, h / 2, w * 0.6)
        vig.setColorAt(0, QColor(30, 15, 50, 30))
        vig.setColorAt(1, QColor(0, 0, 0, 80))
        painter.fillRect(QRectF(0, 0, w, h), vig)

        # ─── Animated glow orb behind title ───
        orb_x = w / 2 + math.sin(self._phase * 0.7) * 20
        orb_y = 85 + math.cos(self._phase * 0.5) * 8
        orb_r = 60 + math.sin(self._phase) * 15
        orb_grad = QRadialGradient(orb_x, orb_y, orb_r)
        orb_grad.setColorAt(0, QColor(120, 80, 230, 50))
        orb_grad.setColorAt(0.4, QColor(60, 140, 220, 25))
        orb_grad.setColorAt(0.7, QColor(100, 50, 200, 15))
        orb_grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, w, h), orb_grad)

        # ─── Title: 天照 ───
        painter.setFont(QFont(self._title_font, 42))
        painter.setPen(QColor(255, 255, 255, 220))
        title_rect = QRectF(0, 30, w, 80)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, "天照")

        # ─── Subtitle ───
        painter.setFont(QFont(self._mc_font, 11))
        painter.setPen(QColor(160, 120, 210, 180))
        sub_rect = QRectF(0, 110, w, 25)
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignCenter, "AMATERASU  LAUNCHER")

        # ─── Decorative line ───
        line_y = 142
        line_grad = QLinearGradient(40, 0, w - 40, 0)
        line_grad.setColorAt(0, QColor(0, 0, 0, 0))
        line_grad.setColorAt(0.2, QColor(100, 50, 200, 140))
        line_grad.setColorAt(0.4, QColor(140, 80, 230, 180))
        t = (math.sin(self._phase) + 1) / 2
        line_grad.setColorAt(0.5, QColor(60, 170, 230, int(180 + t * 75)))
        line_grad.setColorAt(0.6, QColor(140, 80, 230, 180))
        line_grad.setColorAt(0.8, QColor(100, 50, 200, 140))
        line_grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(40, line_y, w - 80, 2), line_grad)

        # ─── Status text ───
        painter.setFont(QFont("Segoe UI", 10))
        painter.setPen(QColor(160, 130, 200, 180))
        status_rect = QRectF(0, 155, w, 25)
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, self._status_text)

        # ─── Progress bar background ───
        bar_x, bar_y, bar_w, bar_h = 40, 192, w - 80, 12
        bar_rect = QRectF(bar_x, bar_y, bar_w, bar_h)

        # Bar background
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 10, 35, 200))
        painter.drawRoundedRect(bar_rect, 6, 6)

        # Bar border
        painter.setPen(QPen(QColor(80, 40, 140, 100), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(bar_rect, 6, 6)

        # ─── Progress bar fill ───
        if self._progress > 0.005:
            fill_w = max(12, self._progress * bar_w)
            fill_rect = QRectF(bar_x, bar_y, fill_w, bar_h)

            fill_grad = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
            fill_grad.setColorAt(0, QColor(60, 20, 130))
            fill_grad.setColorAt(0.3, QColor(80, 60, 200))
            fill_grad.setColorAt(0.5, QColor(50, 140, 220))
            # Shimmer effect
            shimmer = (math.sin(self._phase * 2) + 1) / 2
            fill_grad.setColorAt(0.7, QColor(
                int(60 + shimmer * 40),
                int(160 + shimmer * 30),
                int(230 + shimmer * 25)
            ))
            fill_grad.setColorAt(1, QColor(100, 200, 240))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill_grad)
            painter.drawRoundedRect(fill_rect, 6, 6)

            # Glow on leading edge
            edge_x = bar_x + fill_w
            edge_glow = QRadialGradient(edge_x, bar_y + bar_h / 2, 20)
            edge_glow.setColorAt(0, QColor(80, 160, 240, 120))
            edge_glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.fillRect(QRectF(edge_x - 20, bar_y - 10, 40, bar_h + 20), edge_glow)

        # ─── Percentage ───
        pct = int(self._progress * 100)
        painter.setFont(QFont(self._mc_font, 9))
        painter.setPen(QColor(200, 170, 240, 200))
        pct_rect = QRectF(0, bar_y + bar_h + 6, w, 20)
        painter.drawText(pct_rect, Qt.AlignmentFlag.AlignCenter, f"{pct}%")

        # ─── Particles ───
        for p in self._particles:
            alpha = int(p.life * 200)
            color = QColor(140, 120, 240, alpha) if random.random() > 0.4 else QColor(60, 170, 230, alpha)
            glow = QRadialGradient(p.x, p.y, p.size * 2.5)
            glow.setColorAt(0, QColor(120, 180, 255, alpha))
            glow.setColorAt(0.5, color)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setBrush(glow)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(
                p.x - p.size, p.y - p.size,
                p.size * 2, p.size * 2
            ))

        # ─── Border ───
        painter.setClipping(False)
        border_grad = QLinearGradient(0, 0, w, h)
        border_grad.setColorAt(0, QColor(100, 50, 200, 70))
        border_grad.setColorAt(0.5, QColor(60, 160, 230, 50))
        border_grad.setColorAt(1, QColor(100, 50, 200, 70))
        painter.setPen(QPen(border_grad, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 16, 16)

        # ─── Version in corner ───
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QColor(100, 70, 150, 100))
        painter.drawText(QRectF(0, h - 25, w - 15, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         "v1.0")

        painter.end()
