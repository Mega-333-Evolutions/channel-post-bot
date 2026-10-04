# Channel Post Bot

A Telegram bot (Telethon, bot-token login) for creating and managing channel posts with link buttons,
modelled on Channel Help, plus a few extras: episode-range auto-suggest, `/replace`, and `/repost` (rebuild a whole
channel in order). Data lives in PostgreSQL (SQLite for local use). Errors are reported to a Telegram chat.

## What it does

| Area | Features |
|---|---|
| Posts | create text / photo / video / GIF / audio / file posts, formatting kept, preview, publish, drafts |
| Edit later | text or caption, replace media, add / delete buttons, change button text, change button link, reorder buttons (⬅️⬆️⬇️➡️), link-preview toggle |
| My posts | `/posts` lists drafts and published posts per channel, edit or delete them. **⬅️ Prev** is always shown; on the first page it jumps to the last page. Owners also get **🔧 Check buttons** (see below) |
| Auto-suggest | `/new` offers the next batch: after "Download Episodes 01 to 20" it proposes "Download Episodes 21 to 40", then 41 to 60 ... Tap **Use suggestion** and only the button link is asked |
| `/replace` | swaps `@old` → `@new` inside t.me links (button links, hyperlinks, typed links) in **every post of every channel**, also posts not made by this bot. Preview first, full undo. Works only where Telegram lets the bot edit the post |
| `/repost` | copies **every post of a channel, in order**, to the end of the channel (links swapped on the way), registers the copies in My posts, verifies every button, then deletes the old posts after your confirmation. You pick the channel with buttons |
| Userbot (optional) | a helper user account that deletes the old posts the bot itself is not allowed to delete (older than 48 hours) |
| Error log | every error goes to a Telegram chat as a Python code block with the full traceback |

## Setup (local)

1. Create a bot with @BotFather and copy the token.
2. Get `API_ID` and `API_HASH` at https://my.telegram.org (API development tools).
3. Database: create a free Postgres (for example at neon.tech) and copy its connection string into `DATABASE_URL`.
   Leave it empty to use a local SQLite file.
4. `cp .env.example .env`, fill it in (`OWNER_IDS` = your numeric Telegram id, ask @userinfobot).
5. `pip install -r requirements.txt` then `python bot.py`.
6. Add the bot to each channel as **admin** with: *Post messages*, *Edit messages of others*, *Delete messages*.
   For the userbot clean-up below it also needs *Invite users via link* and *Add new admins*.
7. In the bot: `/addchannel`, then forward a post from the channel (or send its @username).
   Private channel? Post something in the channel while the bot is running, then forward that post.
8. Add the bot to the error-log chat (see **Error log** below).

## Settings

All settings are environment variables (a `.env` file works locally; on a host use its secrets / variables page).

| Name | Meaning |
|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | Telegram credentials (required) |
| `OWNER_IDS` | your numeric Telegram id(s), comma separated (required). Owners can do everything |
| `ADMIN_IDS` | ids that may create / edit posts but not use the owner tools |
| `DATABASE_URL` | PostgreSQL URL (Neon works), empty = local SQLite |
| `DB_POOL` | `null` (default, short connections, lets serverless Postgres sleep) or `queue` |
| `ERROR_LOG_CHAT_ID` | chat that receives the error reports. Empty = `-1002525172451`, `0` or `off` = switched off |
| `USERBOT_SESSION` | session text of the helper account (see **Userbot**). Empty = no userbot |
| `USERBOT_KEEP_ADMIN` | `true` = the userbot stays an admin after a clean-up (default: the right is taken away again) |
| `SESSION_PATH` | where the bot keeps its login. Empty = a temporary folder outside the app folder (on purpose) |
| `SESSION_STRING` | optional, from `python make_session.py` |
| `EDIT_DELAY` | seconds between edits / copies (default 1.2, minimum 0.3) |
| `REPLACE_TYPED_LINKS` | also swap links typed as plain text (default true) |
| `PORT` | the bot answers `ok` on this port (not needed with the Docker setup below) |
| `LOG_LEVEL` | `INFO` (default) |

## Hugging Face Space + GitHub (Docker)

The `Dockerfile` is the one you supplied: it installs the requirements, starts `python -m http.server 7860` in the
background (so the Space has something listening on its port) and runs `python bot.py`.
`.github/workflows/hf-sync.yml` (also yours) copies the repository into the Space `Mega-Evolutions/ButtonBot`
on every push to `main`, leaving the Space's own `README.md` alone.

- Put **all** settings into the Space's *Settings → Variables and secrets* as secrets. The GitHub repository
  needs one secret, `HF_TOKEN` (a Hugging Face token with write access).
- `python -m http.server` publishes every file in `/app` to anyone who knows the Space address. So nothing secret
  may be inside the image: the repository must not contain `.env`, `*.session` or database files (`.gitignore` and
  `.dockerignore` keep them out), and the bot keeps its login in the temporary folder, not in `/app`.
  Do not set `PORT=7860` as well; the bot would have nothing to add and just logs that the port is taken.
- The temporary folder is wiped on every restart. That costs nothing: a bot logs in again with its token.
- Free Spaces can sleep when nobody visits them. A sleeping Space does not run the bot.

## `/replace`: swap a username in links

`/replace @old @new` shows what would change, nothing is edited until you press **Apply**. Try it small first:
`/replace @old @new --last 50`, look at a changed post, `/undo` if it is wrong, then run it for everything.
If Telegram refuses to edit posts made by another bot (the preview and the result say so), use `/repost`.

Options: `--channel @name` · `--last N` (newest N post ids) · `--posts` (also change `t.me/old/123` post links; skipped
by default) · `--no-typed` (leave links typed in the text alone). Only the username changes:
`https://t.me/old?start=XYZ` → `https://t.me/new?start=XYZ`. Other bots' buttons (reactions) are kept as they are.
Every edited post is saved in the database ("adopted"), so it shows up in `/posts`.

## `/repost`: rebuild a channel in order

Use it when Telegram won't let the bot edit another bot's buttons. The copies are posted by this bot, so from then on
every post and button is editable here.

1. `/repost @old @new` (leave the usernames out to copy without changing links). **Pick the channel with the
   buttons** that appear. There is no `--channel` option any more.
2. The bot reads the channel and shows a plan: how many posts, which links change, what is not kept.
   Trial first: `/repost @old @new --last 3` copies only the newest 3 post ids; look at them at the end of the channel,
   then tap **Undo** (removes just the copies).
3. **Start copying.** One post per ~1-2 s, **Stop** and **Continue** work without copying anything twice. Don't post
   in the channel meanwhile.
4. Look at the end of the channel (formatting, buttons, media, albums).
5. **Delete the old posts** (asks again) or **Undo**. Old posts are only offered for deletion after a complete copy,
   and only the ones that have a copy are deleted. Re-pin pinned posts by hand.

How it copies:

- Posts that need no change (no buttons, no link to swap) are copied by Telegram itself: no "forwarded from" header,
  formatting, quotes, spoilers, media and albums stay exactly the same.
- Posts with buttons, or with links to swap, are posted again in **one request** with their final text, entities,
  media and buttons. The buttons are never added in a second step.
- **Every new post is read back from the channel.** If its buttons are not shown, the bot repairs it (a second edit,
  then a two-step edit). If they still do not show, that copy is removed again and the post is reported as
  `ButtonsNotShown`; the original stays untouched and **Try the rest again** retries it.
- If the channel has *Restrict saving content* switched on, forwarding is blocked and the bot rebuilds every post
  from its parts.
- Link buttons are copied with the new username; other bots' buttons (reactions) are not copied because they would be dead.
- Not kept, because Telegram can't: original dates, view counts, reactions, comments, poll votes, pins, links to the
  old posts. Copies are sent silently.

### Deleting the old posts

Telegram's Bot API documentation says bots can delete only messages younger than 48 hours. This bot talks to
Telegram directly and I could not verify whether that limit applies in channels. So: the bot always tries first and
**checks by reading the messages back**. Whatever is still there goes to the userbot (if you set one up).
Without a userbot, the bot tells you what is left and which messages they are; delete those by hand and tap
**Close this repost**.

## 🔧 Check buttons (My posts)

`/posts` → pick a channel → **🔧 Check buttons** (owners only). It compares the buttons saved in the bot with what the
channel really shows for every published post that has link buttons, and lists the posts that are saved with buttons
but show none (or other links). **Fix** puts the saved buttons back. Use it once for channels you reposted with the
earlier version, where some posts showed their buttons in My posts but not in the channel.

## Userbot (optional): deleting posts older than 48 hours

1. On **your own computer**: `pip install telethon`, `python make_userbot_session.py`. Log in with a **separate**
   Telegram account (not your main one). It prints `USERBOT_SESSION=...`.
2. Put that text into the Space / host secrets as `USERBOT_SESSION`. Whoever has it can act as that account; you can end
   it any time in Telegram → Settings → Devices.
3. In the bot, `/userbot` shows whether it is connected.

What happens when the bot cannot delete something (nothing happens before that, and only for that channel):

1. The bot makes a one-time invite link (1 hour, 1 use, titled "Cleanup helper"); if it may not, it uses the channel's
   own invite link; for a public channel the userbot joins by its @username.
2. The userbot joins. A channel that makes new members wait for approval needs you to approve the request once.
3. If the userbot may not delete there and the bot has the *Add new admins* right, the bot makes it an admin with
   only **Delete messages**. Otherwise the message tells you which right to give, or add the userbot yourself.
4. The userbot deletes (100 per request) and the bot checks by reading back.
5. Afterwards the bot takes the admin right away again (unless `USERBOT_KEEP_ADMIN=true`) and revokes the temporary
   link. The account stays a member of the channel; leave with it by hand if you like.

The result message lists these steps, so you can see exactly what was done.

## Error log

Every error the bot meets is sent to `ERROR_LOG_CHAT_ID` as a Telegram code block (`python`), like the One Piece
Bounty Bot: `Unhandled exception in background task <unnamed>:` followed by the whole traceback, including
*The above exception was the direct cause of the following exception*. Reported are: errors in handlers, log
records of level ERROR, exceptions nobody caught in background tasks, and a crash of the whole bot (sent right before it stops).

- The bot must be in that chat (member of a group, or admin of a channel with *Post messages*). At start-up the log
  says whether it can reach the chat.
- Long tracebacks are split into several messages `(part 1/3)`, never shortened.
- The bot token, API hash, session texts and the database password are replaced by `***` before anything is sent.
- The same error within a minute is sent once with a counter; at most 12 reports per minute; if the chat refuses the
  bot, reporting pauses for 10 minutes.
- `/testerror` (owner) raises a test error inside a handler, `/testerror task` lets a background task fail.

## Commands

`/new` `/posts` `/cancel` · `/addchannel` `/channels` · owner only: `/replace` `/undo` `/repost` `/userbot`
`/testerror` `/export`

Auto-suggest reads the newest published post of the channel that has a button with numbers in its text
(`Episodes 01 to 20`, `1-12`, `S01E01-E12`, `Episode 05`). It keeps zero-padding and batch size, advances the
same numbers in the post text, carries over the media and asks for new links. It is computed from your
posts, so cancelled drafts don't skip numbers and editing or deleting the last post updates it.

## Good to know

- **Not tested against real Telegram.** The tests use a simulated channel, build and serialise every request with the
  real Telethon, and run on SQLite and on PostgreSQL 16. Try `/repost --last 3` on a test channel first.
- About "buttons missing in the channel although My posts shows them": the cause is not confirmed. The most likely one
  is that a copy made by Telegram's own copy function keeps a keyboard Telegram then treats as "already set", so the
  follow-up edit with the same keyboard was answered "not modified" and ignored. That is why button posts are now
  created with their buttons in one request and read back, and why **Check buttons** exists. If it ever still happens,
  the result says `ButtonsNotShown` instead of staying silent.
- Albums can't carry buttons, so a post is one text or one media message.
- Captions: 1024 characters; text posts: 4096.
- A published media post can't be turned back into plain text (Telegram limitation).
- Saved drafts store Telegram's media reference. If it has expired the bot retries without it; if Telegram
  still refuses, send the media again with 🖼 Media.
- `/replace` reads posts by id, from 1 up to the channel's counter. Big channels take a while; edits are
  spaced by `EDIT_DELAY` seconds to stay inside Telegram's limits.
- Telethon is pinned to 1.45.x: that version changed how inline buttons are represented.

## Tests

`pip install -r requirements-dev.txt && pytest` (SQLite). For PostgreSQL:
`TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db pytest`.
