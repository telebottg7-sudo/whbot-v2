# WHBot v2

Clean **Pyrogram** Telegram bot for **WatchHentai.net**.

### Features
- Paste an **episode URL** → quality selection buttons → download & send
- Paste a **series URL** → preview (thumbnail + episode count) → Download All
- Custom resolver (does **not** rely on yt-dlp)
- Preferred quality setting
- Automatic GoFile fallback when file exceeds Telegram’s ~2 GB limit
- Progress updates while downloading
- Optional user whitelist

### Quick start

```bash
git clone https://github.com/telebottg7-sudo/whbot-v2.git
cd whbot-v2
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API_ID, API_HASH, BOT_TOKEN
python main.py
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | ✅ | From my.telegram.org |
| `API_HASH` | ✅ | From my.telegram.org |
| `BOT_TOKEN` | ✅ | From @BotFather |
| `BOT_OWNER_ID` | recommended | Your Telegram user ID |
| `ALLOWED_USERS` | optional | Comma-separated user IDs (empty = public) |
| `PREFERRED_QUALITY` | optional | Default `1080p` |
| `DOWNLOAD_DIR` | optional | Default `./downloads` |
| `GOFILE_TOKEN` | optional | For large-file uploads |

### Supported URLs

```
https://watchhentai.net/videos/some-episode-slug/
https://watchhentai.net/series/some-series-slug/
```

### Project layout

```
whbot-v2/
├── main.py                 # Bot entry point + handlers
├── providers/
│   └── watchhentai.py      # Resolver + downloader
├── keyboards/
│   └── inline.py           # Quality & series buttons
├── utils/
│   └── gofile.py           # Large-file uploader
├── .env.example
├── requirements.txt
└── README.md
```

### Notes

- The resolver follows the same pipeline documented in the original WHBot:
  episode page → `data-primary-player-url` → player page → `whJwSources` → custom Base64/XOR decode.
- Series “Download All” uses the preferred quality for every episode.
- Only one active download task is kept per user (new request cancels the previous one).

### License

Private / personal use. Do not redistribute the bot token or credentials.
