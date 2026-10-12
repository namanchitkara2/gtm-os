"""The auth boundary as it actually stands, asserted so it cannot regress quietly.

Two properties, both currently true, both easy to break by accident:

1. ``resolve_identity`` **fails closed.** Outside dev mode it raises rather than
   returning a default principal. This is the property that makes a misconfigured
   deployment serve nobody instead of serving every anonymous caller the dev
   tenant's data — a far worse failure, and one nothing else would catch.

2. The legacy ``/bridge/pair/approve`` is **dev-mode only.** It trusts
   ``tenant_id`` from its request body, which deployed is an account-takeover
   primitive: a bridge is a live LinkedIn session, and attaching one to a
   workspace you named is all it would take.

Neither test needs Postgres, a network or an identity provider, so there is no
excuse for them to be skipped in CI.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from starlette.requests import Request


@pytest.fixture
def app_module():
    import gtmos_control.app as module
    return module


class TestIdentityFailsClosed:
    async def test_no_dev_mode_means_no_principal(self, app_module):
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError) as exc:
                await app_module.resolve_identity()
        assert "No authenticated principal" in str(exc.value)

    async def test_dev_mode_must_be_exactly_one(self, app_module):
        """Gated on an explicit value, not on truthiness of the variable.

        'false', '0' and 'no' are all things a deployment sets when it means off,
        and every one of them is truthy as a string.
        """
        for value in ("0", "false", "no", "", "true", "yes", "1 "):
            with mock.patch.dict(os.environ, {"GTMOS_DEV_MODE": value}, clear=True):
                if value == "1":
                    continue
                with pytest.raises(PermissionError):
                    await app_module.resolve_identity()

    async def test_dev_mode_resolves_the_seeded_workspace(self, app_module):
        with mock.patch.dict(os.environ, {"GTMOS_DEV_MODE": "1"}, clear=True):
            tenant_id, user_id = await app_module.resolve_identity()
        assert str(tenant_id) == "11111111-1111-1111-1111-111111111111"
        assert user_id is not None

    async def test_identity_is_not_read_from_the_request(self, app_module):
        """A caller must not be able to name its own tenant.

        resolve_identity takes no arguments at all, which is the strongest form of
        this guarantee: there is no parameter for a client to supply.
        """
        import inspect
        params = inspect.signature(app_module.resolve_identity).parameters
        assert not params, f"resolve_identity accepts caller input: {list(params)}"


class TestLegacyApproveRouteIsGated:
    def _request(self) -> Request:
        return Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "path": "/bridge/pair/approve", "headers": [], "query_string": b"",
            "client": ("203.0.113.9", 51234), "server": ("testserver", 80),
            "scheme": "http",
        })

    async def test_refused_outside_dev_mode(self, app_module):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = await app_module.pair_approve(self._request())
        assert response.status_code == 404, (
            "a route that trusts tenant_id from its body must not be reachable "
            "in a deployment"
        )

    async def test_refusal_names_the_safe_alternative(self, app_module):
        import json
        with mock.patch.dict(os.environ, {"GTMOS_DEV_MODE": "0"}, clear=True):
            response = await app_module.pair_approve(self._request())
        body = json.loads(response.body)
        assert body["error"] == "route_disabled"
        assert "/api/bridge/pair/approve" in body["use_instead"]

    async def test_it_does_not_touch_the_database_when_refusing(self, app_module):
        """The refusal must precede any DB work, or it is a DoS vector."""
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(app_module.db, "system", side_effect=AssertionError("DB touched")):
            response = await app_module.pair_approve(self._request())
        assert response.status_code == 404
