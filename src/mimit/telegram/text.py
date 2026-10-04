"""Bound Telegram text by UTF-16 units without splitting Unicode characters."""


def valid_message(value: object) -> bool:
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
        return 1 <= len(value.encode("utf-16-le")) // 2 <= 4096
    except UnicodeEncodeError:
        return False


def bounded_text(value: str, limit: int) -> str:
    if len(value.encode("utf-16-le")) // 2 <= limit:
        return value
    visible: list[str] = []
    units = 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit - 1:
            break
        visible.append(char)
        units += width
    return "".join(visible) + "…"
