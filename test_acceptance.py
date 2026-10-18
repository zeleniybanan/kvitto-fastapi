"""Additional acceptance checks against the assignment's public API contract."""

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_api import create_payment, request_data

from app.main import create_app


@pytest.mark.parametrize(
    "tariff,price", [("basic", 990000), ("standard", 1990000), ("premium", 2990000)]
)
@pytest.mark.parametrize("method", ["card", "sbp"])
@pytest.mark.parametrize("promo", [None, "KVITTO10", "kvitto10", "KvItTo10"])
def test_every_immediate_payment_price(client, tariff, price, method, promo):
    payment = create_payment(client, tariff_id=tariff, method=method, promo_code=promo)
    expected_discount = price // 10 if promo else 0
    assert payment["amount"] == price - expected_discount
    assert payment["discount"] == expected_discount
    assert type(payment["amount"]) is int
    assert type(payment["discount"]) is int
    assert payment["status"] == "pending"
    assert payment["schedule"] is None
    assert payment["installment_months"] is None
    assert str(UUID(payment["id"])) == payment["id"]
    assert datetime.fromisoformat(payment["created_at"])


def test_exact_tariff_response_contract(client):
    tariffs = client.get("/tariffs").json()
    assert len(tariffs) == 3
    for tariff in tariffs:
        assert set(tariff) == {"id", "title", "price"}
        assert isinstance(tariff["title"], str) and tariff["title"]
        assert type(tariff["price"]) is int


@pytest.mark.parametrize("field", ["tariff_id", "email", "method"])
@pytest.mark.parametrize("missing", [True, False])
def test_required_payment_fields(client, field, missing):
    body = request_data()
    if missing:
        del body[field]
    else:
        body[field] = None
    response = client.post("/payments", json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    assert client.get("/payments").json() == []


@pytest.mark.parametrize("months", [None, -1, 0, 1, 4, 9, 24, "three", True])
def test_invalid_installment_terms(client, months):
    response = client.post(
        "/payments", json=request_data(method="installment", installment_months=months)
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert client.get("/payments").json() == []


@pytest.mark.parametrize("key", ["", "x" * 256])
def test_invalid_key_does_not_create_row(client, key):
    response = client.post("/payments", json=request_data(), headers={"Idempotency-Key": key})
    assert response.status_code == 422
    assert client.get("/payments").json() == []


def test_longest_key_and_replay_after_status_change(client):
    headers = {"Idempotency-Key": "x" * 255}
    original = client.post("/payments", json=request_data(), headers=headers)
    assert original.status_code == 201
    payment_id = original.json()["id"]
    assert (
        client.post(
            "/webhooks/bank", json={"payment_id": payment_id, "status": "succeeded"}
        ).status_code
        == 200
    )
    replay = client.post(
        "/payments", json=request_data(tariff_id="premium", method="sbp"), headers=headers
    )
    assert replay.status_code == 200
    assert replay.json() == {**original.json(), "status": "succeeded"}
    assert len(client.get("/payments").json()) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"payment_id": "missing"},
        {"status": "succeeded"},
        {"payment_id": "missing", "status": "unknown"},
    ],
)
def test_invalid_webhook_fields(client, body):
    response = client.post("/webhooks/bank", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("route", ["/payments", "/webhooks/bank"])
def test_malformed_json_is_standard_422(client, route):
    response = client.post(route, content=b"{broken", headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"
    assert client.get("/payments").json() == []


def test_sqlite_really_stores_integer_kopecks(client, tmp_path):
    payment = create_payment(client, method="installment", installment_months=3)
    with sqlite3.connect(tmp_path / "test.db") as connection:
        amount, discount, amount_type, discount_type, schedule = connection.execute(
            "SELECT amount, discount, typeof(amount), typeof(discount), schedule FROM payments WHERE id=?",
            (payment["id"],),
        ).fetchone()
        assert (amount, discount) == (1990000, 0)
        assert (amount_type, discount_type) == ("integer", "integer")
        assert json.loads(schedule) == [663334, 663333, 663333]
        assert (
            connection.execute(
                "SELECT count(*) FROM tariffs WHERE typeof(price)='integer'"
            ).fetchone()[0]
            == 3
        )


@pytest.mark.parametrize("wanted", ["pending", "succeeded", "failed", "refunded"])
def test_status_filter_returns_only_requested_status(client, wanted):
    expected_id = None
    for state, path in [
        ("pending", []),
        ("succeeded", ["succeeded"]),
        ("failed", ["failed"]),
        ("refunded", ["succeeded", "refunded"]),
    ]:
        payment_id = create_payment(client)["id"]
        for status in path:
            assert (
                client.post(
                    "/webhooks/bank", json={"payment_id": payment_id, "status": status}
                ).status_code
                == 200
            )
        if state == wanted:
            expected_id = payment_id
    response = client.get("/payments", params={"status": wanted})
    assert response.status_code == 200
    assert [(p["id"], p["status"]) for p in response.json()] == [(expected_id, wanted)]


def test_signed_malformed_body_has_no_side_effect(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "acceptance-secret")
    with TestClient(create_app(f"sqlite:///{tmp_path / 'signed-invalid.db'}")) as client:
        body = b"{broken"
        signature = hmac.new(b"acceptance-secret", body, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/bank",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        assert response.status_code == 422
        assert client.get("/payments").json() == []
