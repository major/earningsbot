#!/usr/bin/env python
"""Send earnings reports to Discord."""
import functools
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from time import sleep

import requests
from discord_webhook import DiscordEmbed, DiscordWebhook

# The Discord webhook URL where messages should be sent. For threads, append
# ?thread_id=1234567890 to the end of the URL.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# Allow messages to be forced on the first run after a restart.
FORCED_MESSAGES = os.environ.get("FORCED_MESSAGES", 0)

# Store the ID of the last message we saw.
last_message_id = 0

# Set up logging.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s;%(levelname)s;%(message)s",
)


@dataclass
class EarningsPublisher(object):
    """Send earnings events to Discord."""

    message: dict

    @functools.cached_property
    def body(self) -> str:
        """Get the message body."""
        return self.message.get("body", "")

    @functools.cached_property
    def _consensus(self) -> str | None:
        """Get consensus for the earnings as a string."""
        regex = r"consensus was (\(?\$[0-9\.]+\)?)"
        result = re.findall(regex, self.body)

        # Some earnings reports for smaller stocks don't have a consensus.
        if not result:
            return None

        # Parse the consensus and handle negative numbers.
        consensus = re.findall(r"[0-9\.]+", result[0])[0]
        if "(" in result[0]:
            # We have an expected loss.
            return f"-${consensus}"

        # We have an expected gain.
        return consensus

    @property
    def consensus(self) -> float | None:
        """Get consensus for the earnings."""
        return float(self._consensus) if self._consensus else None

    @functools.cached_property
    def _earnings(self) -> str | None:
        """Get earnings or loss data as a string."""
        # Look for positive earnings by default.
        regex = r"reported (?:earnings of )?\$([0-9\.]+)"

        # Sometimes there's a loss. 😞
        if "reported a loss of" in self.body:
            regex = r"reported a loss of \$([0-9\.]+)"

        result = re.findall(regex, self.body)

        if result:
            return result[0]

        return None

    @property
    def earnings(self) -> float | None:
        """Get earnings or loss data."""
        return float(self._earnings) if self._earnings else None

    @functools.cached_property
    def ticker(self) -> str | None:
        """Extract ticker from the tweet text."""
        result = re.findall(r"^\$([A-Z]+)", self.body)

        if result:
            return result[0].upper()

        return None

    @property
    def winner(self) -> bool | None:
        """Return an emoji based on the earnings outcome."""
        if not self.consensus:
            return None

        if self.earnings > self.consensus:
            return True

        return False

    @property
    def color(self) -> str:
        """Return a color for the Discord message."""
        if self.winner is None:
            return "aaaaaa"

        if self.winner:
            return "008000"

        return "d42020"

    @property
    def logo(self) -> str:
        """Return a URL for the company logo."""
        url_base = "https://s3.amazonaws.com/logos.atom.finance/stocks-and-funds"
        return f"{url_base}/{self.ticker}.png"

    @property
    def title(self) -> str:
        """Generate a title for the Discord message."""
        return f"{self.ticker}: ${self._earnings} vs. ${self._consensus} expected"

    def send_message(self):
        """Publish a Discord message based on the earnings report."""
        webhook = DiscordWebhook(
            url=WEBHOOK_URL, username="EarningsBot", rate_limit_retry=True
        )
        embed = DiscordEmbed(
            title=self.title,
            color=self.color,
        )
        embed.set_author(
            name=self.message["symbols"][0]["title"],
            url=f"https://finance.yahoo.com/quote/{self.ticker}/",
            icon_url=self.logo,
        )
        webhook.add_embed(embed)
        return webhook.execute()


while True:
    # Don't run on weekends.
    if datetime.today().weekday() > 4:
        logging.info("Skipping run due to weekend.")
        sleep(3600)
        continue

    # Get a list of messages on StockTwits.
    URL = "https://api.stocktwits.com/api/2/streams/user/epsguid.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"
    }
    params = {"filter": "all", "limit": 21}
    logging.info("Getting latest messages...")
    resp = requests.get(URL, params=params, headers=headers)

    # Skip reporting if this is the first time we've run since a restart.
    if last_message_id == 0 and FORCED_MESSAGES == 0:
        logging.info("Not reporting on first run.")
        last_message_id = resp.json()["messages"][0]["id"]

    # Loop over the messages and report on each that hasn't been seen previously.
    for message in reversed(resp.json()["messages"]):
        if message["id"] > last_message_id:
            earnings_publisher = EarningsPublisher(message)
            if earnings_publisher.earnings:
                earnings_publisher.send_message()

            logging.info(earnings_publisher.title)

            # Store this message for next time.
            last_message_id = message["id"]

    logging.info("Waiting 5 minutes before next run...")
    sleep(300)
