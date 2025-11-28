# PULSE | CLUB

Poker tournament management system with real-time timer, rating system, event scheduling, and Telegram bot integration.

## Features

- **Real-time Timer**: Poker tournament blind timer with configurable levels
- **Rating System**: Track player rankings and tournament results
- **Event Scheduling**: Weekly calendar for scheduling poker, mafia, and free play events
- **Telegram Integration**: User registration and broadcast messaging via Telegram bot
- **Admin Panel**: Manage tournaments, events, and send notifications

## Setup

### Local Development

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set environment variables (optional, defaults provided):
```bash
export ADMIN_TOKEN=your-admin-token
export TELEGRAM_BOT_TOKEN=your-telegram-bot-token
export PORT=8000
```

3. Run the server:
```bash
./start.sh
```

Or manually:
```bash
python app.py
```

### Production Deployment

#### Environment Variables

**Required:**
- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token (get from @BotFather)
- `ADMIN_TOKEN`: Secret token for admin access
- `PORT`: Server port (default: 8000)
- `DB_DIR`: Database directory for persistent storage (default: current directory)

**Important:** Never commit `TELEGRAM_BOT_TOKEN` or `ADMIN_TOKEN` to version control. Set them in your hosting platform's environment variables.

#### Amvera Deployment

1. Push code to GitHub
2. Connect repository to Amvera
3. Set environment variables in Amvera dashboard:
   - `TELEGRAM_BOT_TOKEN`: Your Telegram bot token
   - `ADMIN_TOKEN`: Your admin token
   - `DB_DIR`: `/data` (for persistent storage)
4. Deploy

#### Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Get your bot token
3. Set `TELEGRAM_BOT_TOKEN` environment variable
4. Configure webhook (optional, for automatic user collection):
   - Webhook URL: `https://your-domain.com/api/telegram/webhook`
   - Use the admin panel or Telegram Bot API to set the webhook

## API Endpoints

### Public
- `GET /` - Main dashboard
- `GET /rating` - Rating page
- `GET /timer` - Timer page
- `GET /contacts` - Contacts page

### Admin (requires `token` parameter)
- `GET /api/telegram/users?token=...` - Get registered Telegram users
- `GET /api/telegram/users/export?token=...` - Export users as CSV
- `POST /api/telegram/broadcast` - Send broadcast message to all users
- `POST /api/telegram/setup-webhook` - Setup Telegram webhook

## Security Notes

- Admin token is required for all admin operations
- Telegram bot token must be set via environment variable, never hardcoded
- Database is stored in SQLite format
- For production, use strong `ADMIN_TOKEN` and keep it secret

## License

Private project - All rights reserved
