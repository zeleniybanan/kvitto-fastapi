"""FastAPI entry point: create with `uvicorn app.main:app --reload`."""

import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.logic import ALLOWED_TRANSITIONS, price_with_discount, split_installments
from app.models import Payment, Tariff
from app.schemas import BankWebhook, BankWebhookResult, PaymentCreate, PaymentOut, Status, TariffOut

TARIFFS = (
    ("basic", "Basic", 990_000),
    ("standard", "Standard", 1_990_000),
    ("premium", "Premium", 2_990_000),
)


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./payments.db")
    engine = create_engine(
        url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
    )
    session_factory = sessionmaker(engine, expire_on_commit=False)
    webhook_secret = os.getenv("WEBHOOK_SECRET", "")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
            )
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            with session_factory() as session:
                for tariff_id, title, price in TARIFFS:
                    if session.get(Tariff, tariff_id) is None:
                        session.add(Tariff(id=tariff_id, title=title, price=price))
                session.commit()
            yield
        finally:
            engine.dispose()

    application = FastAPI(title="Квитто: Payment API", lifespan=lifespan)

    def get_session():
        with session_factory() as session:
            yield session

    async def verify_signature(
        request: Request,
        signature: str | None = Header(default=None, alias="X-Signature"),
    ) -> None:
        if not webhook_secret:
            return
        expected = hmac.new(
            webhook_secret.encode(), await request.body(), hashlib.sha256
        ).hexdigest()
        if signature is None or not hmac.compare_digest(expected.encode(), signature.encode()):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    @application.get("/tariffs", response_model=list[TariffOut])
    def get_tariffs(session: Session = Depends(get_session)):
        return session.scalars(select(Tariff).order_by(Tariff.price)).all()

    @application.post("/payments", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
    def create_payment(
        payload: PaymentCreate,
        response: Response,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=255
        ),
        session: Session = Depends(get_session),
    ):
        if idempotency_key is not None:
            existing = session.scalar(
                select(Payment).where(Payment.idempotency_key == idempotency_key)
            )
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return existing

        tariff = session.get(Tariff, payload.tariff_id)
        if tariff is None:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "type": "value_error",
                        "loc": ["body", "tariff_id"],
                        "msg": "Unknown tariff",
                        "input": payload.tariff_id,
                    }
                ],
            )

        amount, discount = price_with_discount(tariff.price, payload.promo_code is not None)
        schedule = (
            split_installments(amount, payload.installment_months)
            if payload.installment_months is not None
            else None
        )
        payment = Payment(
            tariff_id=tariff.id,
            amount=amount,
            discount=discount,
            method=payload.method.value,
            installment_months=payload.installment_months,
            schedule=schedule,
            email=payload.email,
            idempotency_key=idempotency_key,
        )
        session.add(payment)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            # Another request might have created the same idempotency key concurrently.
            if idempotency_key is not None:
                existing = session.scalar(
                    select(Payment).where(Payment.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    response.status_code = status.HTTP_200_OK
                    return existing
            raise
        session.refresh(payment)
        return payment

    @application.get("/payments", response_model=list[PaymentOut])
    def list_payments(
        email: str | None = Query(default=None, max_length=254),
        payment_status: Status | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_session),
    ):
        query = select(Payment)
        if email is not None:
            query = query.where(Payment.email == email)
        if payment_status is not None:
            query = query.where(Payment.status == payment_status.value)
        return session.scalars(
            query.order_by(Payment.created_at, Payment.id).limit(limit).offset(offset)
        ).all()

    @application.get("/payments/{payment_id}", response_model=PaymentOut)
    def get_payment(payment_id: str, session: Session = Depends(get_session)):
        payment = session.get(Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return payment

    @application.post(
        "/webhooks/bank", response_model=BankWebhookResult, dependencies=[Depends(verify_signature)]
    )
    def bank_webhook(payload: BankWebhook, session: Session = Depends(get_session)):
        payment = session.get(Payment, payload.payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payload.status.value not in ALLOWED_TRANSITIONS[payment.status]:
            return JSONResponse(status_code=409, content={"error": "invalid_transition"})

        # Compare-and-swap protects the transition from simultaneous webhook calls.
        changed = session.execute(
            update(Payment)
            .where(Payment.id == payment.id, Payment.status == payment.status)
            .values(status=payload.status.value)
        )
        if changed.rowcount != 1:
            session.rollback()
            return JSONResponse(status_code=409, content={"error": "invalid_transition"})
        session.commit()
        return {"result": "ok"}

    return application


app = create_app()
