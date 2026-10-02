# Channel Post Bot

A Telegram bot (Telethon, bot-token login) for creating and managing channel posts with link buttons,
modelled on Channel Help, plus a few extras: episode-range auto-suggest and `/replace`.
Data lives in PostgreSQL (SQLite for local use).

## What it does

| Area | Features |
|---|---|
| Posts | create text / photo / video / GIF / audio / file posts, formatting kept, preview, publish, drafts |
| Edit later | text or caption, replace media, add / delete buttons, change button text, change button link, reorder buttons (⬅️⬆️⬇️➡️), link-preview toggle |
| My posts | `/posts` lists drafts and published posts per channel, edit or delete them |
| Auto-suggest | `/new` offers the next batch: after "Download Episodes 01 to 20" it proposes "Download Episodes 21 to 40", then 41 to 60 ... Tap **Use suggestion** and only the button link is asked |
| `/replace` | swaps `@old` → `@new` inside t.me links (button links, hyperlinks, typed links) in **every post of every channel**, also posts not made by this bot. Preview first, full undo. Works only where Telegram lets the bot edit the post (check with `/testedit` and `/selftest`) |
| `/repost` | copies **every post of a channel, in order**, to the end of the channel (links swapped on the way), registers the copies in My posts, then deletes the old posts after your confirmation. For posts whose buttons another bot owns and Telegram won't let the bot edit |

## Setup

1. Create a bot with @BotFather and copy the token.
2. Get `API_ID` and `API_HASH` at https://my.telegram.org (API development tools).
3. Database: create a free Postgres (for example at neon.tech) and copy its connection string into `DATABASE_URL`.
   Leave it empty to use a local SQLite file.
4. `cp .env.example .env`, fill it in (`OWNER_IDS` = your numeric Telegram id, ask @userinfobot).
5. `pip install -r requirements.txt` then `python bot.py`  (or build the `Dockerfile`).
6. Add the bot to each channel as **admin** with: *Post messages*, *Edit messages of others*, *Delete messages*.
7. In the bot: `/addchannel`, then forward a post from the channel (or send its @username).
   Private channel? Post something in the channel while the bot is running, then forward that post.

## First-run checklist for `/replace` on Channel Help posts

0. `/selftest` posts a silent test message with a button, edits it, reads it back and deletes it: proves the bot's
   own rights work.
1. `/testedit https://t.me/yourchannel/123` with a post that has a link button. It adds `x=1` to the first
   button link, changes it back at once and tells you whether Telegram allowed it.
   If it says *refused*, this bot can't edit those posts: don't ban Channel Help yet.
2. `/replace @old @new --last 50`. Shows what would change, nothing is edited until you press **Apply**.
3. Look at a changed post. Wrong? `/undo`.
4. Run the full `/replace @old @new`.
5. Only now unlink and ban Channel Help. Its reaction buttons keep showing on old posts but no longer work:
   open the post in `/posts`, tap the 🔒 button, **Delete**.

`/replace` options: `--channel @name` · `--last N` (newest N post ids) · `--posts` (also change `t.me/old/123`
post links; skipped by default) · `--no-typed` (leave links typed in the text alone).
Only the username changes: `https://t.me/old?start=XYZ` → `https://t.me/new?start=XYZ`. Other bots' buttons
(reactions) are kept as they are. Every edited post is saved in the database ("adopted"), so it shows up in `/posts`.

## Rebuilding a channel with `/repost`

Use it when `/testedit` says Telegram won't let the bot edit another bot's buttons. The copies are posted by this
bot, so from then on every post and button is editable here.

1. Trial: `/repost @old @new --last 3`. Copies only the newest 3 post ids. Look at them at the end of the channel,
   then tap **Undo** (removes just the copies).
2. Full run: `/repost @old @new`. Shows a plan (how many posts, which links change, what is not kept) and waits.
   Tap **Start copying**. It runs for a while (one post per ~1-2 s) and has a **Stop** button; **Continue** resumes
   without copying anything twice. Don't post in the channel meanwhile.
3. Check the end of the channel (formatting, buttons, media, albums).
4. **Delete the old posts** (asks again) or **Undo**. Old posts are only offered for deletion after a complete copy,
   and only the ones that have a copy are deleted.
5. Re-pin pinned posts by hand.

How it copies: Telegram copies each post itself (no "forwarded from" header), so bold, links, quotes, expandable
quotes, spoilers, media and albums stay exactly the same. If the channel has *Restrict saving content* switched
on, forwarding is blocked and the bot rebuilds each post from its parts with the same entities and media.
Link buttons are copied with the new username; other bots' buttons (reactions) are not copied because they would
be dead. Not kept, because Telegram can't: original dates, view counts, reactions, comments, poll votes, pins,
links to the old posts. Copies are sent silently.

## Commands

`/new` `/posts` `/cancel` · `/addchannel` `/channels` · owner only: `/replace` `/undo` `/testedit` `/selftest` `/repost` `/export`

Auto-suggest reads the newest published post of the channel that has a button with numbers in its text
(`Episodes 01 to 20`, `1-12`, `S01E01-E12`, `Episode 05`). It keeps zero-padding and batch size, advances the
same numbers in the post text, carries over the media and asks for new links. It is computed from your
posts, so cancelled drafts don't skip numbers and editing or deleting the last post updates it.

## Good to know

- Telegram's Bot API documentation says bots can delete only posts younger than 48 hours. This bot talks to
  Telegram directly and in channels that limit may not apply; I could not verify it. If a delete is refused,
  delete by hand and use *Only forget it in the bot*. `/repost` checks every delete and tells you what remains.
- Albums can't carry buttons, so a post is one text or one media message.
- Captions: 1024 characters; text posts: 4096.
- A published media post can't be turned back into plain text (Telegram limitation).
- Saved drafts store Telegram's media reference. If it has expired the bot retries without it; if Telegram
  still refuses, send the media again with 🖼 Media.
- `/replace` reads posts by id, from 1 up to the channel's counter. Big channels take a while; edits are
  spaced by `EDIT_DELAY` seconds to stay inside Telegram's limits.
- Telethon is pinned to 1.45.x: that version changed how inline buttons are represented.
- On hosts that wipe files on restart, use an external database (already the default) and, optionally,
  `python make_session.py` → `SESSION_STRING`. Set `PORT` if the host needs an open port.

## Tests

`pip install -r requirements-dev.txt && pytest` (SQLite). For PostgreSQL:
`TEST_DATABASE_URL=postgresql://user:pass@localhost/db pytest`.
The tests use a fake Telegram client and serialise every request the bot builds with the real Telethon,
but nothing here talks to Telegram itself: check the flows once on a test channel first.
