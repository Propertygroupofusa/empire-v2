import unittest
from unittest.mock import patch

import crypto_btc_compound_bot as bot


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.post_count = 0
        self.detail_count = 0
        self.fills_count = 0

    def post(self, url, **kwargs):
        self.post_count += 1
        return _Response(
            200,
            {"success": True, "success_response": {"order_id": "order-123"}},
        )

    def get(self, url, **kwargs):
        if "/historical/fills" in url:
            self.fills_count += 1
            return _Response(
                200,
                {
                    "fills": [
                        {"order_id": "order-123", "size": "2", "price": "0.21"},
                        {"order_id": "order-123", "size": "1", "price": "0.24"},
                    ]
                },
            )

        self.detail_count += 1
        return _Response(503, {})


class _DetailFillSession(_Session):
    def get(self, url, **kwargs):
        if "/historical/fills" in url:
            self.fills_count += 1
            return _Response(200, {"fills": []})

        self.detail_count += 1
        return _Response(
            200,
            {
                "order": {
                    "status": "FILLED",
                    "filled_size": "3",
                    "filled_value": "0.66",
                }
            },
        )


class _NoFillSession(_Session):
    def get(self, url, **kwargs):
        if "/historical/fills" in url:
            self.fills_count += 1
            return _Response(200, {"fills": []})

        self.detail_count += 1
        return _Response(200, {"order": {"status": "OPEN"}})


class CoinbaseOrderConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot._last_order_error.clear()

    async def _place(self, session):
        order = {"product_id": "ARB-USD", "side": "SELL"}
        with (
            patch.object(bot.asyncio, "sleep", return_value=None),
            patch.object(bot, "_auth_headers", return_value={}),
        ):
            return await bot._place_and_confirm(
                session,
                "/api/v3/brokerage/orders",
                order,
            )

    async def test_reconciles_accepted_order_from_fill_history(self):
        session = _Session()
        fill = await self._place(session)

        self.assertEqual(session.post_count, 1)
        self.assertEqual(session.detail_count, 10)
        self.assertEqual(session.fills_count, 1)
        self.assertIsNotNone(fill)
        self.assertEqual(fill[0], 3.0)
        self.assertAlmostEqual(fill[1], 0.22)

    async def test_preserves_normal_order_detail_confirmation(self):
        session = _DetailFillSession()

        fill = await self._place(session)

        self.assertEqual(session.post_count, 1)
        self.assertEqual(session.detail_count, 1)
        self.assertEqual(session.fills_count, 0)
        self.assertEqual(fill, (3.0, 0.22))

    async def test_records_error_when_accepted_order_cannot_be_confirmed(self):
        session = _NoFillSession()

        fill = await self._place(session)

        self.assertIsNone(fill)
        self.assertEqual(session.post_count, 1)
        self.assertEqual(session.detail_count, 10)
        self.assertEqual(session.fills_count, 1)
        self.assertIn("order-123", bot._last_order_error["ARB-USD"])


if __name__ == "__main__":
    unittest.main()