# Mubasher Telegram Monitor

A GitHub Actions monitor for Mubasher Egypt stock news.

## What it does

- Checks Mubasher stock news every 5 minutes, 24/7.
- Opens each new candidate article and checks the FULL article text.
- Sends direct subscription / subscription-right news.
- For "زيادة رأس المال", sends only when the full article contains positive evidence of a cash subscription/right issue.
- Does NOT use "مجاني" or "مجانية" as a hard exclusion.
- Sends long articles across multiple Telegram messages so Telegram's 4096-character limit is not exceeded.
- Never marks an article as sent until every Telegram message for that article succeeds.
- Keeps sent URLs in `sent_news.json`.
- Produces detailed GitHub Actions logs.
- Includes a heartbeat file so a public scheduled workflow keeps receiving repository activity even during long periods without matching news.

## GitHub Secrets

Repository → Settings → Secrets and variables → Actions → New repository secret

Create:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Never put the bot token directly in the source code.

## Important Telegram setup

The Telegram account must have started a conversation with the bot (send `/start`) before the bot can send private messages to that chat.

If the token was ever exposed publicly, regenerate it with BotFather and update the GitHub secret.

## Run manually

GitHub → Actions → Mubasher Telegram Monitor → Run workflow.

Then open the `Run monitor` step and inspect the log.

Useful log lines:

- `STARTING MUBASHER SCAN`
- `Extracted ... candidate news URLs`
- `MATCH`
- `SENT successfully`
- `IGNORED`
- `Telegram send failed`
- `SCAN SUMMARY`

## Repository visibility

For a 5-minute, 24/7 schedule with GitHub-hosted standard runners at no runner charge, use a PUBLIC repository. Private repositories are subject to the account's Actions-minute allowance.

## Notes

GitHub scheduled workflows are not guaranteed to start at the exact second of every 5-minute interval; GitHub may delay scheduled jobs under load.
