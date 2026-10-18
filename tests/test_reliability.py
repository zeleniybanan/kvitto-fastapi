import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from test_api import create_payment, request_data

from app.main import create_app


@pytest.mark.parametrize(
    "tariff,price", [("basic", 990000), ("standard", 1990000), ("premium", 2990000)]
)
@pytest.mark.parametrize("months", [3, 6, 12])
@pytest.mark.parametrize("promo", [None, "kvitto10"])
def test_all_tariff_installment_totals(client, tariff, price, months, promo):
    payment = create_payment(
        client, tariff_id=tariff, method="installment", installment_months=months, promo_code=promo
    )
    discount = price // 10 if promo else 0
    assert payment["amount"] == price - discount
    assert payment["discount"] == discount
    base, remainder = divmod(price - discount, months)
    assert payment["schedule"] == [base + 1] * remainder + [base] * (months - remainder)


@pytest.mark.parametrize("old", ["pending", "succeeded", "failed", "refunded"])
@pytest.mark.parametrize("new", ["pending", "succeeded", "failed", "refunded"])
def test_entire_transition_matrix(client, old, new):
    payment_id = create_payment(client)["id"]
    paths = {
        "pending": [],
        "succeeded": ["succeeded"],
        "failed": ["failed"],
        "refunded": ["succeeded", "refunded"],
    }
    for intermediate in paths[old]:
        assert (
            client.post(
                "/webhooks/bank", json={"payment_id": payment_id, "status": intermediate}
            ).status_code
            == 200
        )
    response = client.post("/webhooks/bank", json={"payment_id": payment_id, "status": new})
    allowed = (old, new) in {
        ("pending", "succeeded"),
        ("pending", "failed"),
        ("succeeded", "refunded"),
    }
    assert response.status_code == (200 if allowed else 409)
    assert response.json() == ({"result": "ok"} if allowed else {"error": "invalid_transition"})
    assert client.get(f"/payments/{payment_id}").json()["status"] == (new if allowed else old)


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "bad"},
        {"method": "cash"},
        {"promo_code": ""},
        {"promo_code": 10},
        {"installment_months": 3},
        {"tariff_id": "missing"},
        {"method": "installment", "installment_months": 0},
    ],
)
def test_validation_does_not_create_payment(client, overrides):
    response = client.post("/payments", json=request_data(**overrides))
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert client.get("/payments").json() == []


def test_sbp_and_response_contract(client):
    payment = create_payment(client, method="sbp")
    assert set(payment) == {
        "id",
        "status",
        "tariff_id",
        "amount",
        "discount",
        "method",
        "installment_months",
        "schedule",
        "email",
        "created_at",
    }
    assert payment["method"] == "sbp"
    assert payment["installment_months"] is None
    assert payment["schedule"] is None


def test_filters_and_pagination(client):
    first = create_payment(client, email="one@example.org")
    second = create_payment(client, email="two@example.org")
    third = create_payment(client, email="one@example.org")
    client.post("/webhooks/bank", json={"payment_id": first["id"], "status": "succeeded"})
    assert {
        p["id"] for p in client.get("/payments", params={"email": "one@example.org"}).json()
    } == {first["id"], third["id"]}
    assert [
        p["id"]
        for p in client.get(
            "/payments", params={"email": "one@example.org", "status": "pending"}
        ).json()
    ] == [third["id"]]
    assert [p["id"] for p in client.get("/payments", params={"limit": 1, "offset": 1}).json()] == [
        second["id"]
    ]
    assert client.get("/payments", params={"status": "unknown"}).status_code == 422
    assert client.get("/payments", params={"limit": 0}).status_code == 422
    assert client.get("/payments", params={"email": "none@example.org"}).json() == []


def test_concurrent_idempotency_creates_one_row(client):
    barrier = Barrier(6)

    def send(_):
        barrier.wait(timeout=10)
        return client.post("/payments", json=request_data(), headers={"Idempotency-Key": "race"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(send, range(6)))
    assert sorted(r.status_code for r in responses) == [200] * 5 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(client.get("/payments").json()) == 1


def test_concurrent_webhooks_only_one_transition(client):
    payment_id = create_payment(client)["id"]
    barrier = Barrier(2)

    def send(new):
        barrier.wait(timeout=10)
        return client.post("/webhooks/bank", json={"payment_id": payment_id, "status": new})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, ["succeeded", "failed"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert client.get(f"/payments/{payment_id}").json()["status"] in {"succeeded", "failed"}


def test_signature_uses_exact_body_and_rejects_tampering(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    with TestClient(create_app(f"sqlite:///{tmp_path / 'signed.db'}")) as signed:
        payment_id = create_payment(signed)["id"]
        body = json.dumps({"payment_id": payment_id, "status": "succeeded"}).encode()
        signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        for signature_value, payload in [
            (None, body),
            ("wrong", body),
            ("ю", body),
            (signature, body + b" "),
        ]:
            headers = {"Content-Type": "application/json"}
            if signature_value == "ю":
                # HTTP headers are bytes; exercise a non-ASCII signature without client encoding errors.
                headers["X-Signature"] = b"\xff"
            elif signature_value is not None:
                headers["X-Signature"] = signature_value
            response = signed.post("/webhooks/bank", content=payload, headers=headers)
            assert response.status_code == 401
            assert signed.get(f"/payments/{payment_id}").json()["status"] == "pending"
        response = signed.post(
            "/webhooks/bank",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        assert response.status_code == 200
        assert signed.get(f"/payments/{payment_id}").json()["status"] == "succeeded"


def test_restart_preserves_payment_and_tariffs(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    with TestClient(create_app(url)) as first:
        created = first.post(
            "/payments", json=request_data(), headers={"Idempotency-Key": "persist"}
        ).json()
    with TestClient(create_app(url)) as second:
        assert second.get(f"/payments/{created['id']}").json() == created
        assert len(second.get("/tariffs").json()) == 3
        repeated = second.post(
            "/payments", json=request_data(), headers={"Idempotency-Key": "persist"}
        )
        assert repeated.status_code == 200
        assert repeated.json() == created
        assert len(second.get("/payments").json()) == 1


def test_migrations_roundtrip_and_model_agreement(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(url)
    with engine.connect() as connection:
        assert (
            connection.execute(text("select version_num from alembic_version")).scalar() == "0001"
        )
    command.downgrade(config, "base")
    assert "payments" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    assert {"payments", "tariffs"} <= set(inspect(engine).get_table_names())
    engine.dispose()
