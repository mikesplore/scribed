"""Telegram management client for Scribed (Phase 2).

Requires TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, and SCRIBED_API_URL.
"""
import os


def build_application():
    from telegram.ext import Application
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return Application.builder().token(token).build()


if __name__ == "__main__":
    build_application().run_polling()
