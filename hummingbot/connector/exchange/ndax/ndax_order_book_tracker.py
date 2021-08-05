#!/usr/bin/env python
import asyncio
import bisect
import logging

from collections import defaultdict, deque
from typing import Optional, Dict, List, Deque

import hummingbot.connector.exchange.ndax.ndax_constants as CONSTANTS

from hummingbot.core.data_type.order_book_message import OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker import OrderBookTracker
from hummingbot.connector.exchange.ndax.ndax_order_book_message import NdaxOrderBookMessage
from hummingbot.connector.exchange.ndax.ndax_api_order_book_data_source import NdaxAPIOrderBookDataSource
from hummingbot.connector.exchange.ndax.ndax_order_book import NdaxOrderBook
from hummingbot.logger import HummingbotLogger


class NdaxOrderBookTracker(OrderBookTracker):
    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, trading_pairs: Optional[List[str]] = None, domain: Optional[str] = None):
        super().__init__(NdaxAPIOrderBookDataSource(trading_pairs, domain), trading_pairs, domain)

        self._domain = domain
        self._ev_loop: asyncio.BaseEventLoop = asyncio.get_event_loop()
        self._order_book_snapshot_stream: asyncio.Queue = asyncio.Queue()
        self._order_book_diff_stream: asyncio.Queue = asyncio.Queue()
        self._order_book_trade_stream: asyncio.Queue = asyncio.Queue()
        self._process_msg_deque_task: Optional[asyncio.Task] = None
        self._past_diffs_windows: Dict[str, Deque] = {}
        self._order_books: Dict[str, NdaxOrderBook] = {}
        self._saved_message_queues: Dict[str, Deque[NdaxOrderBookMessage]] = \
            defaultdict(lambda: deque(maxlen=1000))
        self._order_book_stream_listener_task: Optional[asyncio.Task] = None
        self._order_book_trade_listener_task: Optional[asyncio.Task] = None

    @property
    def exchange_name(self) -> str:
        """
        Name of the current exchange
        """
        return CONSTANTS.EXCHANGE_NAME

    async def _track_single_book(self, trading_pair: str):
        """
        Update an order book with changes from the latest batch of received messages
        """
        past_diffs_window: Deque[NdaxOrderBookMessage] = deque()
        self._past_diffs_windows[trading_pair] = past_diffs_window

        message_queue: asyncio.Queue = self._tracking_message_queues[trading_pair]
        order_book: NdaxOrderBook = self._order_books[trading_pair]

        last_message_timestamp: float = order_book.snapshot_uid

        diff_messages_accepted: int = 0

        while True:
            try:
                message: NdaxOrderBookMessage = None
                saved_messages: Deque[NdaxOrderBookMessage] = self._saved_message_queues[trading_pair]
                # Process saved messages first if there are any
                if len(saved_messages) > 0:
                    message = saved_messages.popleft()
                else:
                    message = await message_queue.get()

                if message.type is OrderBookMessageType.DIFF:
                    bids, asks = message.bids, message.asks
                    order_book.apply_diffs(bids, asks, message.timestamp)
                    past_diffs_window.append(message)
                    while len(past_diffs_window) > self.PAST_DIFF_WINDOW_SIZE:
                        past_diffs_window.popleft()
                    diff_messages_accepted += 1

                    # Output some statistics periodically.
                    msg_timestamp = message.timestamp
                    if int(msg_timestamp / 60.0) > int(last_message_timestamp / 60.0):
                        self.logger().debug(f"Processed {diff_messages_accepted} order book diffs for {trading_pair}.")
                        diff_messages_accepted = 0
                    last_message_timestamp = msg_timestamp
                elif message.type is OrderBookMessageType.SNAPSHOT:

                    s_bids, s_asks = message.bids, message.asks
                    order_book.apply_snapshot(s_bids, s_asks, message.update_id)
                    last_message_timestamp = msg_timestamp

                    # only replay diffs later than snapshot, first update active order with snapshot then replay diffs
                    past_diffs: List[NdaxOrderBookMessage] = list(past_diffs_window)
                    replay_position = bisect.bisect_right(past_diffs, message)
                    replay_diffs = past_diffs[replay_position:]
                    for diff_message in replay_diffs:
                        d_bids, d_asks = diff_message.bids, diff_message.asks
                        order_book.apply_diffs(d_bids, d_asks, diff_message.update_id)
                        last_message_timestamp = diff_message.timestamp

                    self.logger().debug(f"Processed order book snapshot for {trading_pair}.")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    f"Unexpected error processing order book messages for {trading_pair}.",
                    exc_info=True,
                    app_warning_msg="Unexpected error processing order book messages. Retrying after 5 seconds."
                )
                await asyncio.sleep(5.0)