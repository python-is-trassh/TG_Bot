#!/bin/bash

BOT_DIR="/root/Bot"
BOT_SCRIPT="bot.py"
LOG_FILE="$BOT_DIR/bot_monitor.log"
MAX_RESTARTS=10
SLEEP_INTERVAL=30
TELEGRAM_CHAT_ID="1956781871"
TELEGRAM_BOT_TOKEN="7997085737:AAEKHMKuAUbkCVGjQqrU5Sr0S-ub0WGw0qY"

send_alert() {
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d text="$message" \
        -d parse_mode="Markdown"
}

check_bot() {
    if ! pgrep -f "$BOT_SCRIPT" > /dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Бот не работает. Запускаем..." >> $LOG_FILE
        send_alert "⚠️ *Бот упал!* Перезапускаю..."
        cd $BOT_DIR
        nohup python3 $BOT_SCRIPT >> $LOG_FILE 2>&1 &
        send_alert "✅ *Бот перезапущен!*"
        return 1
    fi
    return 0
}

# Основной цикл
restarts=0
while true; do
    if check_bot; then
        restarts=0
    else
        restarts=$((restarts+1))
        if [ $restarts -ge $MAX_RESTARTS ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') - Достигнут лимит перезапусков" >> $LOG_FILE
            send_alert "🛑 *Достигнут лимит перезапусков бота!* Требуется вмешательство."
            exit 1
        fi
    fi
    sleep $SLEEP_INTERVAL
done
