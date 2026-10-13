"""Idempotent GUI-thread publication into existing Qt widgets."""


def set_widget_value(widget, name: str, value) -> bool:
    getter = "isEnabled" if name == "enabled" else name
    current = widget._full_text if name == "text" and hasattr(widget, "_full_text") else getattr(widget, getter)()
    if current == value:
        return False
    getattr(widget, "set" + name[0].upper() + name[1:])(value)
    return True
