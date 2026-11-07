"""Railway entry point: Flask API thread + Telegram polling."""
import logging
import os
import threading

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def run_flask():
    from api import app
    port = int(os.environ.get("PORT", "5000"))
    logger.info("Flask API starting on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, debug=False,
            use_reloader=False, threaded=True)


def main():
    thread = threading.Thread(target=run_flask, daemon=True, name="flask-api")
    thread.start()
    logger.info("Flask API thread started.")

    # PTB run_polling() manages its own asyncio event loop. It must not be
    # awaited or wrapped in asyncio.run().
    from bot import run_bot
    run_bot()


if __name__ == "__main__":
    main()
