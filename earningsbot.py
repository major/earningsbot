#!/usr/bin/env python
"""Send earnings reports to Discord."""
import logging
import os
import re
import sys
from dataclasses import dataclass
from time import sleep

import requests
from discord_webhook import DiscordEmbed, DiscordWebhook

# The Discord webhook URL where messages should be sent. For threads, append
# ?thread_id=1234567890 to the end of the URL.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

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

    @property
    def consensus(self):
        """Get consensus for the earnings."""
        regex = r"consensus was (\(?\$[0-9\.]+\)?)"
        result = re.findall(regex, self.message["body"])

        # Some earnings reports for smaller stocks don't have a consensus.
        if not result:
            return None

        # Parse the consensus and handle negative numbers.
        raw_consensus = result[0]
        if "(" in raw_consensus:
            # We have an expected loss.
            consensus = float(re.findall(r"[0-9\.]+", raw_consensus)[0]) * -1
        else:
            # We have an expected gain.
            consensus = float(re.findall(r"[0-9\.]+", raw_consensus)[0])

        return consensus

    @property
    def earnings(self):
        """Get earnings or loss data."""
        # Look for positive earnings by default.
        regex = r"reported (?:earnings of )?\$([0-9\.]+)"

        # Sometimes there's a loss. 😞
        if "reported a loss of" in self.message["body"]:
            regex = r"reported a loss of \$([0-9\.]+)"

        result = re.findall(regex, self.message["body"])

        if result:
            return float(result[0])

        return None

    @property
    def ticker(self):
        """Extract ticker from the tweet text."""
        result = re.findall(r"^\$([A-Z]+)", self.message["body"])

        if result:
            return result[0].upper()

        return None

    @property
    def winner(self):
        """Return an emoji based on the earnings outcome."""
        if not self.consensus:
            return None
        elif self.earnings < self.consensus:
            return False
        else:
            return True

    @property
    def color(self):
        """Return a color for the Discord message."""
        if self.winner is None:
            return "aaaaaa"
        elif self.winner:
            return "008000"
        else:
            return "d42020"

    @property
    def logo(self):
        """Return a URL for the company logo."""
        url_base = "https://s3.amazonaws.com/logos.atom.finance/stocks-and-funds"
        return f"{url_base}/{self.ticker}.png"

    @property
    def title(self):
        """Generate a title for the Discord message."""
        return f"{self.ticker}: ${self.earnings} vs. ${self.consensus} expected"

    def send_message(self):
        """Publish a Discord message based on the earnings report."""
        webhook = DiscordWebhook(
            url=WEBHOOK_URL, username="EarningsBot", rate_limit_retry=True
        )
        embed = DiscordEmbed(
            title=self.title,
            description=self.message["symbols"][0]["title"],
            color=self.color,
        )
        embed.set_image("https://major.io/transparent.png")
        embed.set_thumbnail(url=self.logo)
        embed.set_timestamp()
        webhook.add_embed(embed)
        return webhook.execute()


# Print a message to Discord noting that we started up.
logging.info("Starting up...")

while True:
    # Get a list of messages on StockTwits.
    URL = "https://api.stocktwits.com/api/2/streams/user/epsguid.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"
    }
    params = {"filter": "all", "limit": 21}
    resp = requests.get(URL, params=params, headers=headers)

    # Skip reporting if this is the first time we've run since a restart.
    if last_message_id == 0:
        last_message_id = resp.json()["messages"][0]["id"]
        continue

    # Loop over the messages and report on each that hasn't been seen previously.
    for message in reversed(resp.json()["messages"]):
        if message["id"] > last_message_id:
            earnings_publisher = EarningsPublisher(message)
            earnings_publisher.send_message()

            # Store this message for next time.
            last_message_id = message["id"]

    sleep(300)

# Print a message to Discord noting that we shut down.
logging.info("Shutting down...")
