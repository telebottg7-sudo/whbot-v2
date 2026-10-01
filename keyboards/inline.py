from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def quality_keyboard(sources: list[dict], callback_prefix: str = "q") -> InlineKeyboardMarkup:
    """Build quality selection buttons from resolved sources."""
    buttons = []
    for src in sources:
        label = src.get("label", "Unknown")
        # callback_data format: q|<label>  (label is short)
        buttons.append([InlineKeyboardButton(text=f"📥 {label}", callback_data=f"{callback_prefix}|{label}")])
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def series_keyboard(total: int, series_id: str) -> InlineKeyboardMarkup:
    """Simple series actions."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=f"⬇️ Download All ({total} eps)", callback_data=f"series_all|{series_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
    ])


def confirm_keyboard(action: str, payload: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(text="✅ Yes", callback_data=f"confirm|{action}|{payload}"),
            InlineKeyboardButton(text="❌ No", callback_data="cancel"),
        ]
    ])
