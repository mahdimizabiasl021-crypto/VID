# Telegram Video Downloader Bot (Telethon / MTProto, up to ~2GB)

Env vars:
- API_ID, API_HASH (required) - from https://my.telegram.org -> API development tools
- BOT_TOKEN (required) - from @BotFather
- ALLOWED_USERS (optional) - comma-separated Telegram user IDs
- MAX_HEIGHT (optional, default 720) - max video height
- MAX_MB (optional, default 1900) - max file size

Render: New Web Service -> Docker -> set env vars -> Deploy.
Free plan sleeps after inactivity; ping the service URL every 10 min (e.g. UptimeRobot).
