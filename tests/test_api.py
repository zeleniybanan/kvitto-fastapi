import pytest


def request_data(**overrides):
    data = {"tariff_id": "standard", "email": "student@example.org", "method": "card"}
    data.update(overrides)
    return data


def create_payment(client, **overrides):
    response = client.post("/payments", json=request_data(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_tariffs_seeded_in_kopecks(client):
    r = client.get("/tariffs")
    assert r.status_code == 200
    assert [(t["id"], t["price"]) for t in r.json()] == [
        ("basic", 990_000),
        ("standard", 1_990_000),
        ("premium", 2_990_000),
    ]


def test_price_without_promo(client):
    payment = create_payment(client)
    assert (payment["amount"], payment["discount"], payment["status"]) == (1_990_000, 0, "pending")
    assert payment["schedule"] is None


def test_promo_case_insensitive(client):
    for promo in ("KVITTO10", "kvitto10", "KvItTo10"):
        payment = create_payment(client, promo_code=promo)
        assert payment["amount"] == 1_791_000
        assert payment["discount"] == 199_000


def test_unknown_promo_is_422(client):
    response = client.post("/payments", json=request_data(promo_code="NOTVALID"))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "promo_code"]


@pytest.mark.parametrize("months", [3, 6, 12])
def test_installments_sum_exactly_and_extra_kopecks_first(client, months):
    payment = create_payment(client, method="installment", installment_months=months)
    schedule = payment["schedule"]
    assert len(schedule) == months
    assert sum(schedule) == payment["amount"]
    assert max(schedule) - min(schedule) <= 1
    assert schedule == sorted(schedule, reverse=True)
    assert payment["installment_months"] == months


def test_installment_requires_valid_months(client):
    for value in (None, 4):
        response = client.post(
            "/payments", json=request_data(method="installment", installment_months=value)
        )
        assert response.status_code == 422


def test_idempotency_key_returns_same_payment_without_new_creation(client):
    headers = {"Idempotency-Key": "order-123"}
    first = client.post("/payments", json=request_data(), headers=headers)
    second = client.post(
        "/payments", json=request_data(email="different@example.org"), headers=headers
    )
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()


def test_no_key_creates_independent_payments(client):
    assert create_payment(client)["id"] != create_payment(client)["id"]


def test_get_existing_payment(client):
    original = create_payment(client)
    response = client.get(f"/payments/{original['id']}")
    assert response.status_code == 200
    assert response.json() == original


def test_missing_payment_returns_404(client):
    response = client.get("/payments/does-not-exist")
    assert response.status_code == 404


def test_valid_status_transitions(client):
    payment_id = create_payment(client)["id"]
    for value in ("succeeded", "refunded"):
        response = client.post("/webhooks/bank", json={"payment_id": payment_id, "status": value})
        assert response.status_code == 200
        assert response.json() == {"result": "ok"}
        assert client.get(f"/payments/{payment_id}").json()["status"] == value


def test_invalid_transition_is_409_and_status_unchanged(client):
    payment_id = create_payment(client)["id"]
    response = client.post("/webhooks/bank", json={"payment_id": payment_id, "status": "refunded"})
    assert response.status_code == 409
    assert response.json() == {"error": "invalid_transition"}
    assert client.get(f"/payments/{payment_id}").json()["status"] == "pending"


def test_failed_payment_cannot_become_succeeded(client):
    payment_id = create_payment(client)["id"]
    assert (
        client.post(
            "/webhooks/bank", json={"payment_id": payment_id, "status": "failed"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/webhooks/bank", json={"payment_id": payment_id, "status": "succeeded"}
        ).status_code
        == 409
    )
    assert client.get(f"/payments/{payment_id}").json()["status"] == "failed"


def test_webhook_missing_payment_is_404(client):
    response = client.post("/webhooks/bank", json={"payment_id": "missing", "status": "succeeded"})
    assert response.status_code == 404


def test_unknown_tariff_is_422(client):
    response = client.post("/payments", json=request_data(tariff_id="nonexistent"))
    assert response.status_code == 422


def test_noninstallment_does_not_accept_months(client):
    response = client.post("/payments", json=request_data(method="card", installment_months=3))
    assert response.status_code == 422
