import os
import threading
import time
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import create_app
from app.extensions import db
from app.models import (
    Category,
    CustomerAccount,
    CustomerOrderLink,
    EmailMessage,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    ProductVariant,
)
from app.services.orders import update_order_status
from config import TestConfig


def _dedicated_local_postgres_url():
    raw_url = os.environ.get("FITY_ORDER_CONCURRENCY_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set FITY_ORDER_CONCURRENCY_TEST_DATABASE_URL to run the PostgreSQL concurrency test.")

    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail("The dedicated order concurrency test URL is invalid.")
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not (url.database or "").endswith("_test")
    ):
        pytest.fail(
            "The order concurrency test requires a local PostgreSQL URL whose database name ends in _test."
        )
    return raw_url


def test_concurrent_completion_serializes_inventory_and_status_email():
    database_url = _dedicated_local_postgres_url()
    app = create_app(TestConfig)
    app.config.update(SQLALCHEMY_DATABASE_URI=database_url)

    order_id = None
    category_id = None
    product_id = None
    variant_id = None
    account_id = None
    reference_suffix = uuid4().hex[:12]
    first_updated = threading.Event()
    second_attempting_update = threading.Event()
    release_first = threading.Event()
    backend_pids = {}
    outcomes = {}
    failures = []

    with app.app_context():
        db.create_all()
        category = Category(name=f"Concurrency {reference_suffix}", slug=f"concurrency-{reference_suffix}")
        product = Product(
            name=f"Concurrency product {reference_suffix}",
            slug=f"concurrency-product-{reference_suffix}",
            category=category,
            price_cents=100,
            stock_quantity=10,
            reserved_quantity=2,
        )
        variant = ProductVariant(
            product=product,
            size="M",
            color="Blue",
            stock_quantity=8,
            reserved_quantity=3,
        )
        account = CustomerAccount(
            email=f"concurrency-{reference_suffix}@example.test",
            display_name="Concurrency Test",
            email_verified_at=datetime.utcnow(),
        )
        order = Order(
            reference=f"FITY-C-{reference_suffix}",
            fulfilment_method="Collection",
            status="Ready for collection",
            payment_status="Paid",
            total_cents=500,
        )
        order.items.extend([
            OrderItem(
                product=product,
                product_name=product.name,
                quantity=2,
                price_cents=100,
                subtotal_cents=200,
            ),
            OrderItem(
                product=product,
                variant=variant,
                product_name=product.name,
                variant_label="M / Blue",
                quantity=3,
                price_cents=100,
                subtotal_cents=300,
            ),
        ])
        db.session.add_all([category, product, account, order])
        db.session.flush()
        link = CustomerOrderLink(account_id=account.id, order_id=order.id)
        db.session.add(link)
        db.session.commit()
        order_id = order.id
        category_id = category.id
        product_id = product.id
        variant_id = variant.id
        account_id = account.id

    def complete_order(worker_name):
        try:
            with app.app_context():
                session = db.session()
                backend_pids[worker_name] = session.execute(
                    text("SELECT pg_backend_pid()")
                ).scalar_one()
                order = session.get(Order, order_id)
                if worker_name == "second":
                    if not first_updated.wait(15):
                        raise TimeoutError("The first completion did not reach its locked transaction.")
                    second_attempting_update.set()

                changes = update_order_status(
                    order,
                    status="Completed",
                    payment_status="Paid",
                )
                session.flush()
                event = None
                if changes:
                    event = (
                        OrderStatusHistory.query
                        .filter_by(order_id=order_id, field="status", new_value="Completed")
                        .order_by(OrderStatusHistory.id.desc())
                        .first()
                    )

                if worker_name == "first":
                    first_updated.set()
                    if not release_first.wait(15):
                        raise TimeoutError("The first completion was not released to commit.")

                session.commit()
                outcomes[worker_name] = bool(changes)

                if event and worker_name == "first":
                    from app.services.customer_mail import notify_order_event

                    notify_order_event(
                        order,
                        "order_completed",
                        f"order_completed:{event.id}",
                    )
        except Exception as exc:
            failures.append((worker_name, exc))
            with app.app_context():
                db.session.rollback()

    first = threading.Thread(target=complete_order, args=("first",), name="order-completion-first")
    second = threading.Thread(target=complete_order, args=("second",), name="order-completion-second")
    monitor_engine = create_engine(database_url)
    lock_wait_observed = False
    try:
        first.start()
        assert first_updated.wait(15), "The first completion did not update and lock the order."
        second.start()
        assert second_attempting_update.wait(15), "The second completion did not attempt the update."
        assert "second" in backend_pids, "The second request did not establish its own database session."
        assert backend_pids.get("first") != backend_pids["second"], (
            "Concurrent completion requests must use independent PostgreSQL connections."
        )

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with monitor_engine.connect() as connection:
                wait_event_type = connection.execute(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": backend_pids["second"]},
                ).scalar_one_or_none()
            if wait_event_type == "Lock":
                lock_wait_observed = True
                break
            time.sleep(0.05)
    finally:
        release_first.set()
        first.join(timeout=20)
        second.join(timeout=20)
        monitor_engine.dispose()

    assert not first.is_alive() and not second.is_alive(), "Completion workers did not finish."
    assert not failures, "; ".join(f"{worker}: {type(exc).__name__}" for worker, exc in failures)
    assert lock_wait_observed, "The second PostgreSQL session was not observed waiting on the order lock."
    assert outcomes == {"first": True, "second": False}

    with app.app_context():
        order = db.session.get(Order, order_id)
        product = db.session.get(Product, product_id)
        variant = db.session.get(ProductVariant, variant_id)
        assert order.status == "Completed"
        assert product.stock_quantity == 8
        assert product.reserved_quantity == 0
        assert variant.stock_quantity == 5
        assert variant.reserved_quantity == 0
        assert OrderStatusHistory.query.filter_by(
            order_id=order_id,
            field="status",
            new_value="Completed",
        ).count() == 1
        assert EmailMessage.query.filter_by(
            order_id=order_id,
            kind="order_completed",
            status="queued",
        ).count() == 1

        db.session.query(EmailMessage).filter_by(order_id=order_id).delete(synchronize_session=False)
        db.session.query(CustomerOrderLink).filter_by(order_id=order_id).delete(synchronize_session=False)
        db.session.query(OrderStatusHistory).filter_by(order_id=order_id).delete(synchronize_session=False)
        db.session.query(OrderItem).filter_by(order_id=order_id).delete(synchronize_session=False)
        db.session.query(Order).filter_by(id=order_id).delete(synchronize_session=False)
        db.session.query(ProductVariant).filter_by(id=variant_id).delete(synchronize_session=False)
        db.session.query(Product).filter_by(id=product_id).delete(synchronize_session=False)
        db.session.query(Category).filter_by(id=category_id).delete(synchronize_session=False)
        db.session.query(CustomerAccount).filter_by(id=account_id).delete(synchronize_session=False)
        db.session.commit()
