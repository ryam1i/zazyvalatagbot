# Zazyvala Tag Bot

A Telegram bot designed to mention all active group members using clickable emojis and custom messages.

## Features

- **Group Mentions:** Mention active group members in batches with custom or random emojis.
- **Custom Emojis:** Users can assign themselves a unique emoji for mentions.
- **Opt-out:** Users can temporarily exclude themselves from being tagged.
- **Multi-language Support:** Group-level language configuration for interface and commands.

---

## Commands

| Command | Triggers | Description |
| :--- | :--- | :--- |
| `/call [message]` | `/call`, `call` | Tag all active members (with optional message) |
| `/all` | `/all` | Tag all active group members |
| `/setme <emoji>` | `/setme <emoji>`, `setme <emoji>` | Claim a personal emoji for mentions |
| `/me` | `/me`, `me` | Check your currently assigned emoji |
| `/unreg` | `/unreg`, `unreg` | Temporarily opt out of being tagged |
| `/language` | `/language` | Configure group language (Group Admins only) |
| `/support` | `/support` | Support project with Telegram Stars |
| `/backup` | `/backup` | Create a database backup (Bot Admin only) |
| `/broadcast` | Reply with `/broadcast [chat_id]` | Broadcast a message (Bot Admin only) |

---

## How It Works

1. Add @zazyvalatagbot to your Telegram group.
2. Select the group language using `/language`.
3. The bot registers members as they send messages in the chat.
4. Use `/call` or `/all` anytime to mention all active members.
