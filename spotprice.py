# spotprice - A maubot plugin to receive electricity spot prices.
# Copyright (C) 2024 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Type
from datetime import datetime, timedelta

import pytz
from yarl import URL

from mautrix.types import RoomID
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin, MessageEvent
from maubot.handlers import command


CET = pytz.timezone("CET")
nordpool_base_url = URL("https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices")
nordpool_headers = {
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Origin": "https://data.nordpoolgroup.com",
    "Referer": "https://data.nordpoolgroup.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
}


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("delivery_area")
        helper.copy("currency")
        helper.copy("timezone")
        helper.copy("post_to_rooms")
        helper.copy("day_names")
        helper.copy("command")
        helper.copy("vat")


class SpotPriceBot(Plugin):
    delivery_area: str
    currency: str
    timezone: pytz.timezone
    rooms: list[RoomID]
    day_names: list[str]
    vat_multiplier: float
    command_name: str

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        self.config.load_and_update()
        self._schedule_poll()
        self.on_external_config_update()

    def on_external_config_update(self) -> None:
        self.delivery_area = self.config["delivery_area"]
        self.currency = self.config["currency"]
        try:
            self.timezone = pytz.timezone(self.config["timezone"])
        except pytz.UnknownTimeZoneError:
            self.log.warning(f"Unknown timezone {self.config['timezone']}, using UTC")
            self.timezone = pytz.utc
        self.rooms = self.config["post_to_rooms"]
        self.day_names = self.config["day_names"]
        self.vat_multiplier = 1 + self.config["vat"]
        self.command_name = self.config["command"]

    @property
    def next_announce_time(self) -> datetime:
        now = datetime.now().astimezone(CET)
        # Hourly clearing prices are announced at 12:45 CET or later
        # https://www.nordpoolgroup.com/en/the-power-market/Day-ahead-market/
        # Run the first check 5 minutes after that time
        announce_time = now.replace(hour=12, minute=50, second=0, microsecond=0)
        if announce_time < now:
            announce_time += timedelta(days=1)
        return announce_time

    def _schedule_poll(self) -> None:
        announce_time = self.next_announce_time
        now = datetime.now().astimezone(CET)
        announce_in = (announce_time - now).total_seconds()
        self.log.debug(
            f"Scheduling next poll in {announce_in} seconds (now: {now}, at: {announce_time})"
        )
        next_date = (announce_time + timedelta(days=1)).strftime("%Y-%m-%d")
        self.sched.run_later(announce_in, self._scheduled_poll(next_date))

    async def _scheduled_poll(self, date: str) -> None:
        self._schedule_poll()
        await self._do_poll(date)

    async def fetch_prices(self, date: str) -> list[tuple[datetime, float]]:
        fetch_url = nordpool_base_url.with_query(
            {
                "date": date,
                "market": "DayAhead",
                "deliveryArea": self.delivery_area,
                "currency": self.currency,
            }
        )
        resp = await self.http.get(fetch_url, headers=nordpool_headers)
        resp.raise_for_status()
        if resp.status == 204:
            raise Exception("No data available yet")
        try:
            data: list[tuple[datetime, float]] = []
            resp_data = await resp.json()
            for item in resp_data["multiAreaEntries"]:
                price = item["entryPerArea"]["FI"]
                if not isinstance(price, float):
                    raise ValueError(f"Price is a {price} not a float")
                data.append(
                    (
                        datetime.fromisoformat(item["deliveryStart"]),
                        # Convert €/MWh excluding VAT to c/kWh including VAT
                        price * self.vat_multiplier / 10,
                    )
                )
            return data
        except Exception as e:
            raise Exception("Failed to parse prices") from e

    async def _do_poll(self, date: str, attempts: int = 0) -> None:
        if attempts >= 24:
            self.log.error("Failed to fetch spot prices 24 times in a row, giving up")
            return
        try:
            data = await self.fetch_prices(date)
        except Exception:
            self.log.exception("Failed to fetch spot prices, retrying in 5 minutes")
            self.sched.run_later(300, self._do_poll(date, attempts + 1))
            return
        formatted_prices = self._format_prices(data)
        for room_id in self.rooms:
            await self.client.send_markdown(
                room_id, formatted_prices, extra_content=self._format_data(data),
            )

    def _format_data(self, data: list[tuple[datetime, float]]):
        return {
            "bot.mau.spotprice": [
                {"date": date.isoformat(), "cents": int(price * 100)}
                for date, price in data
            ]
        }

    def _format_prices(self, data: list[tuple[datetime, float]]) -> str:
        lines = []
        lines.append(f"{self.day_names[data[12][0].weekday()]} {data[12][0].strftime("%Y-%m-%d")}")
        lines.append("```")
        if len(data) == 96:
            maxlen = max(len(f"{price:.2f}") for _, price in data)
            tblpad = " " * (maxlen - 3)
            lines.append(f"     {tblpad}:00  {tblpad}:15  {tblpad}:30  {tblpad}:45")
            lines.append("   ┌───────" + "─" * maxlen * 4)
            for ts in range(0, 96, 4):
                start_time = data[ts][0].astimezone(self.timezone).strftime("%H")
                p1, p2, p3, p4 = data[ts][1], data[ts+1][1], data[ts+2][1], data[ts+3][1]
                lines.append(
                    f"{start_time} │"
                    f" {p1:{maxlen}.2f} "
                    f" {p2:{maxlen}.2f} "
                    f" {p3:{maxlen}.2f} "
                    f" {p4:{maxlen}.2f}"
                )
        else:
            for ts, price in data:
                lines.append(f"{ts.astimezone(self.timezone).strftime("%H:%M")} {price:.2f} c/kWh")
        lines.append("```")
        return "\n".join(lines)

    @command.new(lambda self: self.command_name)
    @command.argument("date", required=False, matches=r"\d{4}-\d{2}-\d{2}")
    async def poll_manually(self, evt: MessageEvent, date: str | None = None) -> None:
        if not date:
            date = self.next_announce_time.strftime("%Y-%m-%d")
        try:
            prices = await self.fetch_prices(date)
        except Exception:
            self.log.exception("Failed to fetch spot prices")
            await evt.reply("Failed to fetch prices")
        else:
            await evt.reply(self._format_prices(prices), extra_content=self._format_data(prices))
