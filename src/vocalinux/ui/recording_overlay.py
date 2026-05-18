"""
Floating recording indicator, inspired by VoiceInk on macOS.

Strategy:
  * If the compositor implements wlr-layer-shell (Hyprland, Sway, KDE Plasma
    Wayland) AND GtkLayerShell GI bindings are installed, render a real
    anchored pill at the bottom of the screen. Layer-shell guarantees the
    surface never receives keyboard focus, so text injection still targets
    the user's editor.
  * Otherwise (notably GNOME Mutter, which deliberately refuses layer-shell)
    fall back to a persistent libnotify notification. Notifications are
    rendered by the shell itself, never become a focused toplevel, and
    therefore cannot redirect IBus injection away from the target window.

The visible appearance differs between the two modes, but the invariant
"overlay never steals focus from the dictation target" holds either way.
"""

import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from ..common_types import RecognitionState

logger = logging.getLogger(__name__)


try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell

    _LAYER_SHELL_TYPELIB = True
except (ImportError, ValueError):
    GtkLayerShell = None
    _LAYER_SHELL_TYPELIB = False


def _layer_shell_supported() -> bool:
    """True only if both bindings exist and the compositor honors the protocol."""
    if not _LAYER_SHELL_TYPELIB:
        return False
    try:
        return bool(GtkLayerShell.is_supported())
    except Exception:
        return False


_CSS = b"""
.vocalinux-overlay-box {
    background-color: rgba(20, 20, 20, 0.88);
    border-radius: 18px;
    padding: 8px 16px;
    border: 1px solid rgba(255, 255, 255, 0.08);
}

.vocalinux-overlay-label {
    color: #f5f5f5;
    font-weight: 500;
    font-size: 13px;
}

.vocalinux-overlay-dot {
    background-color: #ff3b30;
    border-radius: 8px;
    min-width: 12px;
    min-height: 12px;
}
"""

_LISTENING_LABEL = "Recording"
_PROCESSING_LABEL = "Transcribing"

_ANIMATION_INTERVAL_MS = 350
_ANIMATION_FRAMES = ("", ".", "..", "...")


class _LayerShellOverlay:
    """Real bottom-anchored pill via wlr-layer-shell."""

    def __init__(self, margin_bottom: int = 80) -> None:
        self._visible = False

        self._window = Gtk.Window()
        self._window.set_decorated(False)
        self._window.set_resizable(False)
        self._window.set_accept_focus(False)
        self._window.set_focus_on_map(False)
        self._window.set_skip_taskbar_hint(True)
        self._window.set_skip_pager_hint(True)
        self._window.set_app_paintable(True)
        self._window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)

        screen = self._window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self._window.set_visual(visual)

        self._install_css()

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.get_style_context().add_class("vocalinux-overlay-box")

        self._dot = Gtk.Box()
        self._dot.get_style_context().add_class("vocalinux-overlay-dot")
        self._dot.set_valign(Gtk.Align.CENTER)
        box.pack_start(self._dot, False, False, 0)

        self._label = Gtk.Label(label=_LISTENING_LABEL)
        self._label.get_style_context().add_class("vocalinux-overlay-label")
        box.pack_start(self._label, False, False, 0)

        self._window.add(box)

        GtkLayerShell.init_for_window(self._window)
        GtkLayerShell.set_layer(self._window, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self._window, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_margin(
            self._window, GtkLayerShell.Edge.BOTTOM, margin_bottom
        )
        GtkLayerShell.set_keyboard_mode(self._window, GtkLayerShell.KeyboardMode.NONE)

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(_CSS)
        except GLib.Error as exc:
            logger.warning("Failed to load overlay CSS: %s", exc)
            return
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def show(self, label: str) -> None:
        self._label.set_label(label)
        if not self._visible:
            self._window.show_all()
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self._window.hide()
            self._visible = False


class _NotificationOverlay:
    """libnotify fallback. Renders in the shell, cannot steal focus."""

    def __init__(self) -> None:
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify

        if not Notify.is_initted():
            Notify.init("Vocalinux")

        self._Notify = Notify
        self._notification = None
        self._visible = False
        self._base_label = ""
        self._frame_index = 0
        self._animation_source_id = 0

    def show(self, label: str) -> None:
        self._base_label = label
        self._frame_index = 0
        try:
            if self._notification is None:
                self._notification = self._Notify.Notification.new(
                    label, None, "audio-input-microphone"
                )
                self._notification.set_timeout(self._Notify.EXPIRES_NEVER)
                self._notification.set_urgency(self._Notify.Urgency.CRITICAL)
                self._notification.set_hint(
                    "resident", GLib.Variant.new_boolean(True)
                )
                self._notification.set_hint(
                    "transient", GLib.Variant.new_boolean(False)
                )
                self._notification.set_hint(
                    "category", GLib.Variant.new_string("device")
                )
            else:
                self._notification.update(label, None, "audio-input-microphone")
            self._notification.show()
            self._visible = True
            self._start_animation()
        except Exception as exc:
            logger.warning("Notification overlay failed: %s", exc)

    def hide(self) -> None:
        self._stop_animation()
        if not self._visible or self._notification is None:
            return
        try:
            self._notification.close()
        except Exception as exc:
            logger.debug("Notification close failed: %s", exc)
        finally:
            self._visible = False

    def _start_animation(self) -> None:
        self._stop_animation()
        self._animation_source_id = GLib.timeout_add(
            _ANIMATION_INTERVAL_MS, self._tick_animation
        )

    def _stop_animation(self) -> None:
        if self._animation_source_id:
            GLib.source_remove(self._animation_source_id)
            self._animation_source_id = 0

    def _tick_animation(self) -> bool:
        if not self._visible or self._notification is None:
            return False
        self._frame_index = (self._frame_index + 1) % len(_ANIMATION_FRAMES)
        suffix = _ANIMATION_FRAMES[self._frame_index]
        try:
            self._notification.update(
                f"{self._base_label}{suffix}", None, "audio-input-microphone"
            )
            self._notification.show()
        except Exception as exc:
            logger.debug("Notification animation tick failed: %s", exc)
            return False
        return True


class RecordingOverlay:
    """Public façade. Picks the right backend and drives it from state changes."""

    def __init__(self, margin_bottom: int = 80) -> None:
        if _layer_shell_supported():
            logger.info("Recording overlay: using layer-shell backend")
            self._backend = _LayerShellOverlay(margin_bottom=margin_bottom)
        else:
            logger.info(
                "Recording overlay: layer-shell unsupported, falling back to "
                "libnotify (avoids focus stealing on GNOME Wayland)"
            )
            self._backend = _NotificationOverlay()

    def on_state_change(self, state: RecognitionState) -> None:
        """Speech-engine callback. Safe to call from any thread."""
        if state == RecognitionState.LISTENING:
            GLib.idle_add(self._backend.show, _LISTENING_LABEL)
        elif state == RecognitionState.PROCESSING:
            GLib.idle_add(self._backend.show, _PROCESSING_LABEL)
        else:
            GLib.idle_add(self._backend.hide)
