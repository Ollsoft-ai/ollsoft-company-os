"""A request body that is not JSON is the caller's mistake: 400, not a 500.

Found by the guided installer on Windows: a generated password carried the
hidden carriage return of a Windows tool, the JSON body was invalid, and
POST /admin/users answered 500 with a traceback in the journal.
"""
import asyncio
import json

import pytest
from aiohttp import web

from kb_platform.hub import _body


class FakeRequest:
    def __init__(self, raw: str):
        self.raw = raw

    async def json(self):
        return json.loads(self.raw)


def test_a_body_with_a_raw_carriage_return_is_a_400():
    with pytest.raises(web.HTTPBadRequest) as caught:
        asyncio.run(_body(FakeRequest('{"username": "john", "password": "abc\rdef"}')))
    assert json.loads(caught.value.text) == {"error": "the request body is not valid JSON"}
    assert caught.value.content_type == "application/json"


def test_a_truncated_body_is_a_400():
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(_body(FakeRequest('{"name": "proj-')))


def test_a_good_body_comes_back_as_it_was_sent():
    assert asyncio.run(_body(FakeRequest('{"name": "proj-x", "n": 2}'))) == {"name": "proj-x", "n": 2}
