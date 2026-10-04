"""Helpers around Telethon types: entity/button (de)serialisation, media info, raw edits, rights."""
from __future__ import annotations

import asyncio
import base64
import copy
import html
import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from telethon import errors, functions, types
from telethon.extensions import BinaryReader

log = logging.getLogger(__name__)
esc = html.escape

# ----------------------------------------------------------------------------- entities
_SIMPLE = {
    "bold": types.MessageEntityBold,
    "italic": types.MessageEntityItalic,
    "underline": types.MessageEntityUnderline,
    "strike": types.MessageEntityStrike,
    "code": types.MessageEntityCode,
}
if hasattr(types, "MessageEntitySpoiler"):
    _SIMPLE["spoiler"] = types.MessageEntitySpoiler

# entities Telegram works out by itself from the text - never stored
_AUTO = tuple(
    getattr(types, n)
    for n in (
        "MessageEntityUrl",
        "MessageEntityMention",
        "MessageEntityHashtag",
        "MessageEntityCashtag",
        "MessageEntityBotCommand",
        "MessageEntityEmail",
        "MessageEntityPhone",
        "MessageEntityBankCard",
    )
    if hasattr(types, n)
)


def _b64(obj: Any) -> str:
    return base64.b64encode(bytes(obj)).decode()


def _unb64(data: str) -> Any:
    return BinaryReader(base64.b64decode(data)).tgread_object()


def ser_entities(entities) -> list:
    """Message entities -> JSON-friendly list (UTF-16 offsets, as Telegram uses)."""
    out: list = []
    for e in entities or []:
        if isinstance(e, _AUTO):
            continue
        done = False
        for key, cls in _SIMPLE.items():
            if isinstance(e, cls):
                out.append({"k": key, "o": e.offset, "l": e.length})
                done = True
                break
        if done:
            continue
        if isinstance(e, types.MessageEntityPre):
            out.append({"k": "pre", "o": e.offset, "l": e.length, "g": e.language or ""})
        elif isinstance(e, types.MessageEntityTextUrl):
            out.append({"k": "url", "o": e.offset, "l": e.length, "u": e.url})
        elif isinstance(e, types.MessageEntityBlockquote):
            out.append({"k": "quote", "o": e.offset, "l": e.length, "c": bool(getattr(e, "collapsed", False))})
        elif isinstance(e, types.MessageEntityCustomEmoji):
            out.append({"k": "emoji", "o": e.offset, "l": e.length, "d": e.document_id})
        else:
            try:  # anything else is kept byte-for-byte
                out.append({"k": "raw", "b": _b64(e)})
            except Exception:  # pragma: no cover
                log.warning("cannot store entity %r", e)
    return out


def de_entities(data) -> list:
    out: list = []
    for d in data or []:
        k = d.get("k")
        try:
            if k in _SIMPLE:
                out.append(_SIMPLE[k](d["o"], d["l"]))
            elif k == "pre":
                out.append(types.MessageEntityPre(d["o"], d["l"], d.get("g", "")))
            elif k == "url":
                out.append(types.MessageEntityTextUrl(d["o"], d["l"], d["u"]))
            elif k == "quote":
                out.append(types.MessageEntityBlockquote(d["o"], d["l"], True if d.get("c") else None))
            elif k == "emoji":
                out.append(types.MessageEntityCustomEmoji(d["o"], d["l"], d["d"]))
            elif k == "raw":
                out.append(_unb64(d["b"]))
        except Exception:  # pragma: no cover
            log.warning("bad stored entity %r", d)
    return out


# ------------------------------------------------------------------------------ buttons
_LEGACY = hasattr(types, "KeyboardButtonUrl")  # Telethon < 1.45 used one class per button kind


def button_url(b) -> Optional[str]:
    """The link of a URL button, else None."""
    if _LEGACY:
        return b.url if isinstance(b, types.KeyboardButtonUrl) else None
    t = getattr(b, "type", None)
    return t.url if isinstance(t, types.InlineButtonTypeUrl) else None


def is_callback_button(b) -> bool:
    if _LEGACY:
        return isinstance(b, types.KeyboardButtonCallback)
    return isinstance(getattr(b, "type", None), types.InlineButtonTypeCallback)


def make_url_button(text: str, url: str, style=None):
    if _LEGACY:
        return types.KeyboardButtonUrl(text, url)
    return types.KeyboardButton(text=text, type=types.InlineButtonTypeUrl(url), style=style)


def with_url(b, new_url: str):
    """Copy of URL button `b` pointing to `new_url`; every other attribute (colour, ...) is kept."""
    nb = copy.copy(b)
    if _LEGACY:
        nb.url = new_url
    else:
        nb.type = copy.copy(b.type)
        nb.type.url = new_url
    return nb


def _style_dump(b) -> Optional[str]:
    st = getattr(b, "style", None)
    if st is None:
        return None
    if not any(getattr(st, f, None) for f in ("bg_primary", "bg_danger", "bg_success", "icon")):
        return None
    try:
        return _b64(st)
    except Exception:  # pragma: no cover
        return None


def ser_markup(markup) -> list:
    """Inline keyboard -> rows of {'t': text, 'u': url} (URL buttons) or {'t', 'raw'} (any other kind)."""
    rows: list = []
    if isinstance(markup, types.ReplyInlineMarkup):
        for row in markup.rows:
            r = []
            for b in row.buttons:
                url = button_url(b)
                if url is not None:
                    item = {"t": b.text, "u": url}
                    s = _style_dump(b)
                    if s:
                        item["s"] = s
                    r.append(item)
                else:
                    try:
                        r.append({"t": getattr(b, "text", ""), "raw": _b64(b)})
                    except Exception:  # pragma: no cover
                        log.warning("cannot store button %r", b)
            if r:
                rows.append(r)
    return rows


def build_markup(rows) -> Optional[types.ReplyInlineMarkup]:
    out = []
    for row in rows or []:
        btns = []
        for b in row:
            if b.get("raw"):
                try:
                    btns.append(_unb64(b["raw"]))
                except Exception:  # pragma: no cover
                    continue
            elif b.get("u"):
                style = None
                if b.get("s"):
                    try:
                        style = _unb64(b["s"])
                    except Exception:  # pragma: no cover
                        style = None
                btns.append(make_url_button(b["t"], b["u"], style))
        if btns:
            out.append(types.KeyboardButtonRow(btns))
    return types.ReplyInlineMarkup(out) if out else None


def state_of(text, entities, markup, preview) -> dict:
    """Snapshot used for the undo log."""
    return {
        "text": text or "",
        "entities": ser_entities(entities),
        "buttons": ser_markup(markup),
        "preview": preview,  # True/False for text messages, None for media messages
    }


# --------------------------------------------------------------------------------- media
def classify_media(m) -> Optional[str]:
    """photo | video | animation | audio | document, or None (text / unsupported)."""
    media = getattr(m, "media", None)
    if isinstance(media, types.MessageMediaPhoto) and getattr(media, "photo", None):
        return "photo"
    if isinstance(media, types.MessageMediaDocument) and getattr(media, "document", None):
        if m.gif:
            return "animation"
        if m.video:
            return "video"
        if m.audio:
            return "audio"
        if m.voice or m.video_note or m.sticker:
            return None
        return "document"
    return None


# Media is stored as the serialised Telegram media object ("tl:<base64>"). Telethon's Bot-API style
# file_id helper cannot pack photos with the current Telegram schema, so we don't rely on it.
_REF_PREFIX = "tl:"


def media_ref(m) -> Optional[str]:
    """A storable reference to the photo / document attached to message `m` (None if it has none)."""
    media = getattr(m, "media", None)
    if isinstance(media, (types.MessageMediaPhoto, types.MessageMediaDocument)):
        try:
            return _REF_PREFIX + _b64(media)
        except Exception:  # pragma: no cover
            log.warning("cannot store media reference", exc_info=True)
    return None


pack_file_id = media_ref  # older name, kept for the callers


def media_from_ref(ref: str, *, blank_reference: bool = False):
    """Rebuild the media object; `blank_reference` drops the (possibly expired) file reference."""
    media = _unb64(ref[len(_REF_PREFIX):]) if ref.startswith(_REF_PREFIX) else None
    if media is None:
        raise ValueError("unknown media reference")
    if blank_reference:
        media = copy.copy(media)
        inner = copy.copy(media.photo if isinstance(media, types.MessageMediaPhoto) else media.document)
        inner.file_reference = b""
        if isinstance(media, types.MessageMediaPhoto):
            media.photo = inner
        else:
            media.document = inner
    return media


async def with_media(ref: str, call):
    """call(media): first with the stored file reference, then (if Telegram says it expired) without it."""
    try:
        return await call(media_from_ref(ref))
    except errors.RPCError as e:
        if "FileReference" not in type(e).__name__ and type(e).__name__ != "MediaEmptyError":
            raise
        log.info("stored file reference rejected (%s) - retrying without it", type(e).__name__)
        return await call(media_from_ref(ref, blank_reference=True))


def media_info(m) -> tuple:
    kind = classify_media(m)
    return kind, (media_ref(m) if kind else None)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------------- misc
def peer_of(ch) -> types.InputPeerChannel:
    return types.InputPeerChannel(ch.id, ch.access_hash)


def post_link(ch, message_id: int) -> str:
    if ch.username:
        return f"https://t.me/{ch.username}/{message_id}"
    return f"https://t.me/c/{ch.id}/{message_id}"


def short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


async def flood_retry(factory: Callable[[], Awaitable[Any]], tries: int = 4) -> Any:
    for _ in range(tries):
        try:
            return await factory()
        except errors.FloodWaitError as e:
            wait = int(getattr(e, "seconds", 5)) + 1
            log.warning("flood wait %ss", wait)
            await asyncio.sleep(min(wait, 900))
    return await factory()


async def edit_raw(client, peer, message_id: int, *, text: Optional[str] = None, entities=None, markup=None, preview=None):
    """messages.editMessage with every field explicit.

    `markup=None` together with `text` removes the keyboard (that is how Telegram behaves);
    a markup-only edit (text=None) leaves the text alone.
    """
    kw: dict = {"peer": peer, "id": message_id}
    if markup is not None:
        kw["reply_markup"] = markup
    if text is not None:
        kw["message"] = text
        kw["entities"] = list(entities or [])
        if preview is not None:
            kw["no_webpage"] = not preview
    return await client(functions.messages.EditMessageRequest(**kw))


async def get_rights(client, ch):
    """The bot's own admin rights in a channel, or None if Telegram won't tell us."""
    try:
        res = await client(
            functions.channels.GetParticipantRequest(channel=peer_of(ch), participant=types.InputPeerSelf())
        )
    except Exception as e:
        log.info("could not read own rights in %s: %s", ch.id, e)
        return None
    p = res.participant
    if isinstance(p, (types.ChannelParticipantCreator, types.ChannelParticipantAdmin)):
        r = getattr(p, "admin_rights", None)
        creator = isinstance(p, types.ChannelParticipantCreator)

        def has(name: str) -> bool:
            return True if (creator and r is None) else bool(getattr(r, name, False))

        return SimpleNamespace(
            admin=True,
            post=has("post_messages"),
            edit=has("edit_messages"),
            delete=has("delete_messages"),
            invite=has("invite_users"),
            add_admins=has("add_admins"),
        )
    return SimpleNamespace(admin=False, post=False, edit=False, delete=False, invite=False, add_admins=False)


_HINTS = {
    "ChatAdminRequiredError": "The bot needs admin rights in that channel (post / edit / delete messages).",
    "MessageAuthorRequiredError": "Telegram wants the message author. Give the bot the 'Edit messages of others' right.",
    "InlineBotRequiredError": "That post was sent through another bot's inline mode, so only that bot can edit it.",
    "MessageEditTimeExpiredError": "Telegram no longer allows editing that message.",
    "MessageIdInvalidError": (
        "Telegram won't let the bot change that message (or it was deleted). If the post is still there, "
        "the usual reason is that its buttons were added by another bot: only that bot may change them."
    ),
    "ButtonUrlInvalidError": "Telegram rejected one of the button links.",
    "ReplyMarkupInvalidError": "Telegram rejected the buttons.",
    "ChatWriteForbiddenError": "The bot can't post in that channel - check its admin rights.",
    "ChannelPrivateError": "The bot can't access that channel any more (removed or banned?).",
    "UserBannedInChannelError": "The bot is banned in that channel.",
    "FileReferenceExpiredError": "The saved media expired - send the media again.",
    "MediaEmptyError": "Telegram could not use that media - send it again.",
    "EntityBoundsInvalidError": "The text formatting doesn't fit the text.",
}


def explain_rpc(e: BaseException) -> str:
    name = type(e).__name__
    return f"Telegram refused ({name}). " + _HINTS.get(name, str(e)[:200])
