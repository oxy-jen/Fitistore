import json
import re
from collections import deque
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from flask import current_app
from sqlalchemy import MetaData, and_, delete, func, inspect, or_, select, text, update
from sqlalchemy import Table

from app.extensions import db


class CustomerCleanupBlocked(Exception):
    pass


def _redact_text(value, identities):
    text = value or ""
    for identity in identities:
        identity = (identity or "").strip()
        if len(identity) >= 3:
            text = re.sub(re.escape(identity), "[redacted]", text, flags=re.IGNORECASE)
    return text


def anonymize_customer_account(
    account_id,
    *,
    admin_id,
    delete_orders=False,
    delete_payment_proofs=False,
    delete_reviews=False,
    delete_referral_history=False,
    delete_product_exposure=False,
    delete_email_drafts=False,
    delete_contact_activity=False,
):
    from app.models import (
        CustomerAccount, CustomerToken, CustomerCart, CustomerOrderLink,
        CustomerProfile, ReferralClick, Referral, LoyaltyTransaction, Review,
        ProductEnquiry, RestockAlert,
        Order, OrderItem, OrderStatusHistory,
        PaymentAttempt, PaymentTransaction, RefundRequest, EmailMessage,
        EmailCampaignRecipient, EmailDraft, EmailDeliveryInvestigation,
        EmailDeliveryInvestigationEvent, EmailLegacyPayloadReviewEvent,
        MarketingProductExposure, MarketingSuppressionCase,
        MarketingSuppressionEvent, FinancialAuditEvent,
    )

    optional_models = (
        CustomerAccount, CustomerToken, CustomerCart, CustomerOrderLink,
        CustomerProfile, ReferralClick, Referral, LoyaltyTransaction, Review,
        ProductEnquiry, RestockAlert,
        Order, OrderItem, OrderStatusHistory, PaymentAttempt,
        PaymentTransaction, RefundRequest, EmailMessage,
        EmailCampaignRecipient, EmailDraft, EmailDeliveryInvestigation,
        EmailDeliveryInvestigationEvent, EmailLegacyPayloadReviewEvent,
        MarketingProductExposure, MarketingSuppressionCase,
        MarketingSuppressionEvent, FinancialAuditEvent,
    )
    inspector = inspect(db.engine)
    available = {}
    for model in optional_models:
        table_name = model.__tablename__
        if not inspector.has_table(table_name):
            available[model] = False
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected = {column.name for column in model.__table__.columns}
        if expected - columns:
            raise CustomerCleanupBlocked(
                f"The {table_name} schema is incomplete; account removal was stopped."
            )
        available[model] = True

    if not available[CustomerAccount]:
        raise CustomerCleanupBlocked("Customer account storage is unavailable.")
    if not available[FinancialAuditEvent]:
        raise CustomerCleanupBlocked("Audit storage is unavailable; account removal was stopped.")

    account = db.session.query(CustomerAccount).filter_by(id=account_id).with_for_update().first()
    if account is None:
        return None
    if account.email.endswith("@redacted.invalid"):
        raise CustomerCleanupBlocked("This account has already been removed.")

    redacted_email = f"removed-customer-{account.id}@redacted.invalid"
    letters_only = uuid4().hex.upper().translate(str.maketrans("0123456789", "ABCDEFGHIJ"))
    redacted_key = f"REMOVED-CUSTOMER-{letters_only[:20]}"
    redacted_profile_code = f"REMOVED-{letters_only[20:32]}"
    identities = [account.email, account.display_name]
    profiles = []
    if available[CustomerProfile]:
        profiles = db.session.query(CustomerProfile).filter_by(account_id=account.id).all()
    profile_ids = [profile.id for profile in profiles]
    profile_phones = {profile.phone.strip().lower() for profile in profiles if profile.phone}
    profile_codes = {profile.referral_code for profile in profiles if profile.referral_code}
    identities.extend(profile.phone for profile in profiles if profile.phone)
    counts = {}
    proof_paths = []

    messages = []
    if available[EmailMessage]:
        messages = (
            db.session.query(EmailMessage)
            .filter(or_(
                EmailMessage.account_id == account.id,
                func.lower(EmailMessage.recipient) == account.email.lower(),
            ))
            .with_for_update()
            .all()
        )
        if any(
            message.status == "sending"
            or (message.claim_token and message.status in {"queued", "sending"})
            for message in messages
        ):
            raise CustomerCleanupBlocked(
                "A message is being dispatched for this account. Wait for the provider attempt to finish, then retry."
            )

    order_ids = []
    orders = []
    if available[CustomerOrderLink]:
        order_ids = [
            row.order_id
            for row in db.session.query(CustomerOrderLink.order_id)
            .filter(CustomerOrderLink.account_id == account.id)
            .all()
        ]
    if order_ids and available[Order]:
        orders = (
            db.session.query(Order)
            .filter(Order.id.in_(order_ids))
            .with_for_update()
            .all()
        )
    linked_referral_ids = []
    linked_loyalty_ids = []
    if order_ids and available[Referral]:
        linked_referral_ids = [
            row.id
            for row in db.session.query(Referral.id)
            .filter(Referral.qualifying_order_id.in_(order_ids))
            .all()
        ]
    if order_ids and available[LoyaltyTransaction]:
        linked_loyalty_ids = [
            row.id
            for row in db.session.query(LoyaltyTransaction.id)
            .filter(LoyaltyTransaction.order_id.in_(order_ids))
            .all()
        ]
    identities.extend(profile.display_name for profile in profiles if profile.display_name)
    identities.extend(order.customer_phone for order in orders if order.customer_phone)
    if available[ProductEnquiry]:
        enquiries = (
            db.session.query(ProductEnquiry)
            .filter(func.lower(ProductEnquiry.contact) == account.email.lower())
            .with_for_update()
            .all()
        )
        if delete_contact_activity:
            for enquiry in enquiries:
                db.session.delete(enquiry)
            counts["product_enquiries"] = len(enquiries)
        else:
            for enquiry in enquiries:
                enquiry.contact = redacted_email
                enquiry.message = _redact_text(enquiry.message, identities)
                enquiry.internal_response = _redact_text(enquiry.internal_response, identities)
                enquiry.consent_to_contact = False

    if available[RestockAlert]:
        alerts = (
            db.session.query(RestockAlert)
            .filter(func.lower(RestockAlert.contact) == account.email.lower())
            .with_for_update()
            .all()
        )
        if delete_contact_activity:
            for alert in alerts:
                db.session.delete(alert)
            counts["restock_alerts"] = len(alerts)
        else:
            for alert in alerts:
                alert.contact = redacted_email
                alert.consent_to_contact = False
                if alert.status in {"pending", "queued"}:
                    alert.status = "cancelled"

        if available[EmailMessage]:
            fresh_messages = (
                db.session.query(EmailMessage)
                .filter(or_(
                    EmailMessage.account_id == account.id,
                    func.lower(EmailMessage.recipient) == account.email.lower(),
                ))
                .with_for_update()
                .all()
            )
            messages = list({message.id: message for message in (*messages, *fresh_messages)}.values())
            if any(
                message.status == "sending"
                or (message.claim_token and message.status in {"queued", "sending"})
                for message in messages
            ):
                raise CustomerCleanupBlocked(
                    "A message is being dispatched for this account. Wait for the provider attempt to finish, then retry."
                )
    for order in orders:
        if order.payment_proof_filename and delete_payment_proofs:
            filename = Path(order.payment_proof_filename)
            if filename.name != order.payment_proof_filename:
                raise CustomerCleanupBlocked("A payment proof has an unsafe filename; no account data was changed.")
            proof_folder = Path(current_app.config["PAYMENT_PROOF_FOLDER"]).resolve()
            proof_path = (proof_folder / filename.name).resolve()
            if proof_path.parent != proof_folder:
                raise CustomerCleanupBlocked("A payment proof path is unsafe; no account data was changed.")
            proof_paths.append(proof_path)

    cart_ids = []
    if available[CustomerCart]:
        cart_ids = [
            row.id
            for row in db.session.query(CustomerCart.id)
            .filter(CustomerCart.account_id == account.id)
            .all()
        ]

    drafts_to_update = []
    drafts_to_delete = []
    if available[EmailDraft]:
        drafts = db.session.query(EmailDraft).all()
        for draft in drafts:
            try:
                recipients = json.loads(draft.account_ids_json or "[]")
            except json.JSONDecodeError:
                if str(account.id) in (draft.account_ids_json or ""):
                    raise CustomerCleanupBlocked(
                        "An email draft has an unreadable recipient list; review it before removing this account."
                    )
                continue
            if not isinstance(recipients, list):
                raise CustomerCleanupBlocked(
                    "An email draft has an invalid recipient list; review it before removing this account."
                )
            has_account = any(str(value) == str(account.id) for value in recipients)
            has_cart = draft.cart_id in cart_ids
            if has_account or has_cart:
                if delete_email_drafts:
                    drafts_to_delete.append(draft)
                else:
                    draft.account_ids_json = json.dumps(
                        [value for value in recipients if str(value) != str(account.id)]
                    )
                    draft.cart_id = None if has_cart else draft.cart_id
                    for field in ("name", "subject", "headline", "message", "blocks_json"):
                        setattr(draft, field, _redact_text(getattr(draft, field), identities))
                    drafts_to_update.append(draft)

    for order in orders:
        order.customer_name = "Deleted customer"
        order.customer_phone = ""
        order.instructions = ""
        order.notes = _redact_text(order.notes, identities)
        order.referral_code = ""
        order.referrer_key = ""
        if delete_payment_proofs:
            order.payment_proof_filename = ""
    if order_ids and available[PaymentAttempt]:
        attempts = db.session.query(PaymentAttempt).filter(PaymentAttempt.order_id.in_(order_ids)).all()
        for attempt in attempts:
            attempt.customer_phone = ""
            attempt.raw_payload = ""
            attempt.investigation_note = _redact_text(attempt.investigation_note, identities)
            attempt.result_description = _redact_text(attempt.result_description, identities)
    if order_ids and available[PaymentTransaction]:
        transactions = db.session.query(PaymentTransaction).filter(
            PaymentTransaction.order_id.in_(order_ids)
        ).all()
        for transaction in transactions:
            transaction.phone = ""
            transaction.raw_payload = ""
            transaction.discrepancy = _redact_text(transaction.discrepancy, identities)
    if order_ids and available[RefundRequest]:
        refunds = db.session.query(RefundRequest).filter(RefundRequest.order_id.in_(order_ids)).all()
        for refund in refunds:
            refund.reason = _redact_text(refund.reason, identities)
            refund.note = _redact_text(refund.note, identities)
    if order_ids and available[OrderStatusHistory]:
        histories = db.session.query(OrderStatusHistory).filter(
            OrderStatusHistory.order_id.in_(order_ids)
        ).all()
        for history in histories:
            history.old_value = _redact_text(history.old_value, identities)
            history.new_value = _redact_text(history.new_value, identities)
            history.note = _redact_text(history.note, identities)
        counts["order_status_histories_redacted"] = len(histories)

    if available[CustomerOrderLink]:
        counts["order_links_preserved"] = db.session.query(CustomerOrderLink).filter_by(
            account_id=account.id
        ).count()
    counts["orders_preserved"] = len(orders)

    if available[CustomerToken]:
        counts["verification_tokens"] = db.session.query(CustomerToken).filter_by(
            account_id=account.id
        ).delete(synchronize_session=False)
    if available[CustomerCart]:
        counts["saved_carts"] = db.session.query(CustomerCart).filter_by(
            account_id=account.id
        ).delete(synchronize_session=False)

    if delete_referral_history:
        if available[ReferralClick] and profile_ids:
            counts["referral_clicks"] = db.session.query(ReferralClick).filter(
                ReferralClick.referrer_id.in_(profile_ids)
            ).delete(synchronize_session=False)
        if available[Referral]:
            referral_filters = []
            if linked_referral_ids:
                referral_filters.append(Referral.id.in_(linked_referral_ids))
            if profile_phones:
                referral_filters.extend((
                    Referral.referrer_key.in_(profile_phones),
                    Referral.referred_key.in_(profile_phones),
                ))
            if profile_codes:
                referral_filters.append(Referral.referral_code.in_(profile_codes))
            if referral_filters:
                counts["referrals"] = db.session.query(Referral).filter(
                    or_(*referral_filters)
                ).delete(synchronize_session=False)
        if available[LoyaltyTransaction] and profile_phones:
            loyalty_filter = func.lower(LoyaltyTransaction.customer_key).in_(profile_phones)
            if linked_loyalty_ids:
                loyalty_filter = or_(loyalty_filter, LoyaltyTransaction.id.in_(linked_loyalty_ids))
            counts["loyalty_transactions"] = db.session.query(LoyaltyTransaction).filter(
                loyalty_filter
            ).delete(synchronize_session=False)
        elif available[LoyaltyTransaction] and linked_loyalty_ids:
            counts["loyalty_transactions"] = db.session.query(LoyaltyTransaction).filter(
                LoyaltyTransaction.id.in_(linked_loyalty_ids)
            ).delete(synchronize_session=False)
    else:
        if available[Referral]:
            referral_filters = []
            if linked_referral_ids:
                referral_filters.append(Referral.id.in_(linked_referral_ids))
            if profile_phones:
                referral_filters.extend((
                    Referral.referrer_key.in_(profile_phones),
                    Referral.referred_key.in_(profile_phones),
                ))
            referrals = db.session.query(Referral).filter(
                or_(*referral_filters)
            ).all() if referral_filters else []
            for referral in referrals:
                if referral.referrer_key.strip().lower() in profile_phones:
                    referral.referrer_key = redacted_key
                if referral.referred_key.strip().lower() in profile_phones:
                    referral.referred_key = redacted_key
                referral.note = ""
        if available[LoyaltyTransaction] and profile_phones:
            loyalty_filter = func.lower(LoyaltyTransaction.customer_key).in_(profile_phones)
            if linked_loyalty_ids:
                loyalty_filter = or_(loyalty_filter, LoyaltyTransaction.id.in_(linked_loyalty_ids))
            transactions = db.session.query(LoyaltyTransaction).filter(loyalty_filter).all()
            for transaction in transactions:
                transaction.customer_key = redacted_key
                transaction.reason = ""
        elif available[LoyaltyTransaction] and linked_loyalty_ids:
            transactions = db.session.query(LoyaltyTransaction).filter(
                LoyaltyTransaction.id.in_(linked_loyalty_ids)
            ).all()
            for transaction in transactions:
                transaction.customer_key = redacted_key
                transaction.reason = ""
        for profile in profiles:
            profile.phone = redacted_key
            profile.display_name = "Deleted customer"
            profile.referred_by_code = ""
            profile.referral_code = redacted_profile_code

    if delete_referral_history and available[CustomerProfile] and profile_ids:
        counts["customer_profiles"] = db.session.query(CustomerProfile).filter(
            CustomerProfile.id.in_(profile_ids)
        ).delete(synchronize_session=False)

    if available[Review]:
        reviews = db.session.query(Review).filter_by(customer_account_id=account.id).all()
        if delete_reviews:
            counts["reviews"] = len(reviews)
            for review in reviews:
                db.session.delete(review)
        else:
            for review in reviews:
                review.customer_account_id = None
                review.display_name = "Deleted customer"
                review.title = _redact_text(review.title, identities)
                review.text = _redact_text(review.text, identities)

    if delete_product_exposure and available[MarketingProductExposure]:
        counts["marketing_product_exposure"] = db.session.query(
            MarketingProductExposure
        ).filter_by(account_id=account.id).delete(synchronize_session=False)
    if available[MarketingSuppressionEvent]:
        for event in db.session.query(MarketingSuppressionEvent).filter_by(account_id=account.id).all():
            event.reason_text = None
            event.metadata_json = None
    if available[MarketingSuppressionCase]:
        for case in db.session.query(MarketingSuppressionCase).filter_by(account_id=account.id).all():
            case.metadata_json = None
            if case.clearance_reason:
                case.clearance_reason = "Redacted after customer account removal."

    redacted_recipient = redacted_email
    if available[EmailCampaignRecipient]:
        recipients = db.session.query(EmailCampaignRecipient).filter_by(account_id=account.id).all()
        for recipient in recipients:
            recipient.recipient = redacted_recipient
            recipient.failure_reason = "Customer data redacted."
            if recipient.status == "queued":
                recipient.status = "suppressed"
        counts["campaign_recipients_redacted"] = len(recipients)

    for message in messages:
        if message.status == "queued" or (
            message.status == "failed" and message.error_category != "ambiguous_outcome"
        ):
            message.status = "suppressed"
        message.next_attempt_at = None
        message.claim_token = None
        message.claimed_at = None
        message.recipient = redacted_recipient
        message.subject = "Customer data redacted"
        message.html_body = None
        message.text_body = None
        message.failure_reason = "Customer data redacted."
    counts["email_messages_redacted"] = len(messages)

    if messages and available[EmailDeliveryInvestigation]:
        message_ids = [message.id for message in messages]
        for investigation in db.session.query(EmailDeliveryInvestigation).filter(
            EmailDeliveryInvestigation.message_id.in_(message_ids)
        ).all():
            investigation.reason = "Customer data redacted."
            investigation.evidence_summary = None
            investigation.evidence_source = None
            if investigation.resolution_explanation:
                investigation.resolution_explanation = "Customer data redacted."
    if messages and available[EmailDeliveryInvestigationEvent] and available[EmailDeliveryInvestigation]:
        message_ids = [message.id for message in messages]
        investigation_ids = [
            row.id
            for row in db.session.query(EmailDeliveryInvestigation.id).filter(
                EmailDeliveryInvestigation.message_id.in_(message_ids)
            ).all()
        ]
        if investigation_ids:
            for event in db.session.query(EmailDeliveryInvestigationEvent).filter(
                EmailDeliveryInvestigationEvent.investigation_id.in_(investigation_ids)
            ).all():
                event.evidence_summary = None
                event.evidence_source = None
                event.explanation = "Customer data redacted."
    if messages and available[EmailLegacyPayloadReviewEvent]:
        message_ids = [message.id for message in messages]
        for event in db.session.query(EmailLegacyPayloadReviewEvent).filter(
            EmailLegacyPayloadReviewEvent.message_id.in_(message_ids)
        ).all():
            event.reason = "Customer data redacted."
            event.provider_evidence = ""

    for draft in drafts_to_delete:
        db.session.delete(draft)
    counts["email_drafts_deleted"] = len(drafts_to_delete)
    counts["email_drafts_redacted"] = len(drafts_to_update)

    account.email = redacted_email
    account.display_name = "Deleted customer"
    account.password_hash = None
    account.google_sub = None
    account.email_verified_at = None
    account.two_factor_enabled = False
    account.marketing_status = "unsubscribed"
    account.marketing_consent_at = None
    account.marketing_withdrawn_at = datetime.utcnow()
    if available[FinancialAuditEvent]:
        for event in db.session.query(FinancialAuditEvent).filter_by(
            entity_type="CustomerAccount", entity_id=account.id
        ).all():
            event.note = _redact_text(event.note, identities)
        if order_ids:
            for event in db.session.query(FinancialAuditEvent).filter(
                FinancialAuditEvent.entity_type == "Order",
                FinancialAuditEvent.entity_id.in_(order_ids),
            ).all():
                event.note = _redact_text(event.note, identities)
    account.active = False

    db.session.add(FinancialAuditEvent(
        admin_id=admin_id,
        event_type="customer_account_anonymized",
        entity_type="CustomerAccount",
        entity_id=account.id,
        note=json.dumps({
            "delete_orders_requested": bool(delete_orders),
            "orders_deleted": 0,
            "orders_preserved": len(orders),
            "delete_payment_proofs": bool(delete_payment_proofs),
            "delete_reviews": bool(delete_reviews),
            "delete_referral_history": bool(delete_referral_history),
            "delete_product_exposure": bool(delete_product_exposure),
            "delete_email_drafts": bool(delete_email_drafts),
            "counts": counts,
        }, sort_keys=True),
    ))
    return {"counts": counts, "proof_paths": proof_paths}


def _permanent_delete_fk_rows(root_ids):
    connection = db.session.connection()
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, autoload_with=connection, resolve_fks=False)
        for name in table_names
    }
    inbound = {}
    for child_name in table_names:
        for foreign_key in inspector.get_foreign_keys(child_name):
            parent_name = foreign_key.get("referred_table")
            local_columns = tuple(foreign_key.get("constrained_columns") or ())
            remote_columns = tuple(foreign_key.get("referred_columns") or ())
            if not parent_name or not local_columns or len(local_columns) != len(remote_columns):
                raise CustomerCleanupBlocked(
                    "The database contains an incomplete relationship; permanent deletion was stopped."
                )
            if parent_name not in tables:
                continue
            inbound.setdefault(parent_name, []).append(
                (child_name, local_columns, remote_columns)
            )
    fallback_relationships = {
        "customer_account": ("account_id", "customer_account_id"),
        "order": ("order_id",),
        "customer_profile": ("referrer_id", "profile_id"),
        "customer_cart": ("cart_id",),
        "email_message": ("message_id", "recovered_message_id"),
        "email_delivery_attempt": (
            "attempt_id",
            "original_attempt_id",
            "related_attempt_id",
            "recovery_of_attempt_id",
        ),
        "payment_attempt": ("payment_attempt_id",),
        "payment_transaction": ("payment_transaction_id",),
        "email_delivery_investigation": ("investigation_id",),
        "marketing_suppression_case": ("suppression_case_id",),
        "customer_consent_event": ("legacy_consent_event_id",),
    }
    for parent_name, child_columns in fallback_relationships.items():
        if parent_name not in tables:
            continue
        for child_name, child in tables.items():
            for column_name in child_columns:
                if column_name not in child.c:
                    continue
                signature = (child_name, (column_name,), ("id",))
                if signature not in inbound.get(parent_name, ()):
                    inbound.setdefault(parent_name, []).append(signature)

    planned = {}
    queue = deque()
    relationships = set()

    def add_row(table_name, row):
        table = tables[table_name]
        primary_key = tuple(table.primary_key.columns)
        if not primary_key:
            raise CustomerCleanupBlocked(
                "A related database table has no primary key; permanent deletion was stopped."
            )
        identity = tuple(row[column.name] for column in primary_key)
        rows = planned.setdefault(table_name, {})
        if identity not in rows:
            rows[identity] = dict(row)
            queue.append((table_name, identity))
        return identity

    for table_name, identities in root_ids.items():
        if table_name not in tables:
            if identities:
                raise CustomerCleanupBlocked(
                    "A related database table is unavailable; permanent deletion was stopped."
                )
            continue
        table = tables[table_name]
        primary_key = tuple(table.primary_key.columns)
        if not primary_key:
            raise CustomerCleanupBlocked(
                "A related database table has no primary key; permanent deletion was stopped."
            )
        for identity in identities:
            if len(identity) != len(primary_key):
                raise CustomerCleanupBlocked(
                    "A related database record could not be identified; permanent deletion was stopped."
                )
            condition = and_(*(
                column == value for column, value in zip(primary_key, identity)
            ))
            for row in connection.execute(
                select(table).where(condition).with_for_update()
            ).mappings():
                add_row(table_name, row)

    while queue:
        parent_name, parent_identity = queue.popleft()
        parent = tables[parent_name]
        parent_row = planned[parent_name][parent_identity]
        for child_name, local_columns, remote_columns in inbound.get(parent_name, ()):
            child = tables[child_name]
            conditions = []
            for local_name, remote_name in zip(local_columns, remote_columns):
                value = parent_row.get(remote_name)
                if value is None:
                    break
                conditions.append(child.c[local_name] == value)
            if len(conditions) != len(local_columns):
                continue
            rows = connection.execute(
                select(child).where(and_(*conditions)).with_for_update()
            ).mappings().all()
            if rows and not child.primary_key.columns:
                raise CustomerCleanupBlocked(
                    "A related database table has no primary key; permanent deletion was stopped."
                )
            for row in rows:
                child_identity = add_row(child_name, row)
                relationships.add((
                    (child_name, child_identity),
                    (parent_name, parent_identity),
                    child_name,
                    local_columns,
                ))

    nodes = {
        (table_name, identity)
        for table_name, rows in planned.items()
        for identity in rows
    }

    def ordered_nodes(ignored_relationships):
        edges = {
            (child_node, parent_node)
            for child_node, parent_node, child_name, local_columns in relationships
            if (child_node, parent_node, child_name, local_columns)
            not in ignored_relationships
        }
        # Edges point from dependent row to referenced row, so dependents delete first.
        indegree = {node: 0 for node in nodes}
        dependents = {}
        for child_node, parent_node in edges:
            indegree[parent_node] += 1
            dependents.setdefault(child_node, []).append(parent_node)
        ready = deque(node for node, degree in indegree.items() if degree == 0)
        result = []
        while ready:
            node = ready.popleft()
            result.append(node)
            for parent_node in dependents.get(node, ()):
                indegree[parent_node] -= 1
                if indegree[parent_node] == 0:
                    ready.append(parent_node)
        return result if len(result) == len(nodes) else None

    order = ordered_nodes(set())
    if order is None:
        nullable_relationships = set()
        for child_node, parent_node, child_name, local_columns in relationships:
            child_table = tables[child_name]
            if all(child_table.c[column_name].nullable for column_name in local_columns):
                nullable_relationships.add(
                    (child_node, parent_node, child_name, local_columns)
                )
        for child_node, _, child_name, local_columns in nullable_relationships:
            table = tables[child_name]
            primary_key = tuple(table.primary_key.columns)
            condition = and_(*(
                column == value
                for column, value in zip(primary_key, child_node[1])
            ))
            connection.execute(
                update(table).where(condition).values({
                    column_name: None for column_name in local_columns
                })
            )
        order = ordered_nodes(nullable_relationships)
    if order is None:
        raise CustomerCleanupBlocked(
            "The related database records have a non-nullable dependency cycle; permanent deletion was stopped."
        )

    deleted_counts = {}
    for table_name, identity in order:
        table = tables[table_name]
        primary_key = tuple(table.primary_key.columns)
        condition = and_(*(
            column == value for column, value in zip(primary_key, identity)
        ))
        connection.execute(delete(table).where(condition))
        deleted_counts[table_name] = deleted_counts.get(table_name, 0) + 1
    return deleted_counts


def permanently_delete_customer_account(account_id, *, admin_id):
    from app.models import (
        CustomerAccount, CustomerProfile, CustomerOrderLink, Order, EmailMessage,
        EmailDeliveryAttempt, CustomerCart, ProductEnquiry, RestockAlert,
        ProductSuggestion, EmailDraft, Referral, LoyaltyTransaction,
        FinancialAuditEvent,
    )

    scoped_models = (
        CustomerAccount, CustomerProfile, CustomerOrderLink, Order, EmailMessage,
        EmailDeliveryAttempt, CustomerCart, ProductEnquiry, RestockAlert,
        ProductSuggestion, EmailDraft, Referral, LoyaltyTransaction,
        FinancialAuditEvent,
    )
    inspector = inspect(db.engine)
    available = {}
    for model in scoped_models:
        table_name = model.__tablename__
        if not inspector.has_table(table_name):
            available[model] = False
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in model.__table__.columns}
        if expected_columns - actual_columns:
            raise CustomerCleanupBlocked(
                f"The {table_name} schema is incomplete; permanent deletion was stopped."
            )
        available[model] = True
    if not available[CustomerAccount]:
        raise CustomerCleanupBlocked("Customer account storage is unavailable.")
    if not available[FinancialAuditEvent]:
        raise CustomerCleanupBlocked(
            "Audit storage is unavailable; permanent deletion was stopped."
        )

    account = (
        db.session.query(CustomerAccount)
        .filter_by(id=account_id)
        .with_for_update()
        .first()
    )
    if account is None:
        return None

    account_email = account.email
    identities = [account_email]
    if account.display_name and account.display_name != "Deleted customer":
        identities.append(account.display_name)
    profiles = []
    if available[CustomerProfile]:
        profiles = CustomerProfile.query.filter_by(account_id=account.id).all()
    profile_phones = {
        profile.phone.strip().lower()
        for profile in profiles
        if profile.phone and not profile.phone.startswith("REMOVED-CUSTOMER-")
    }
    profile_codes = {
        profile.referral_code
        for profile in profiles
        if profile.referral_code and not profile.referral_code.startswith("REMOVED-")
    }
    identities.extend(
        profile.phone for profile in profiles
        if profile.phone and not profile.phone.startswith("REMOVED-CUSTOMER-")
    )
    identities.extend(
        profile.display_name for profile in profiles
        if profile.display_name and profile.display_name != "Deleted customer"
    )

    order_ids = set()
    if available[CustomerOrderLink]:
        order_ids.update(
            order_id
            for (order_id,) in db.session.query(CustomerOrderLink.order_id)
            .filter_by(account_id=account.id)
            .all()
        )
    if available[Order] and profile_phones:
        order_ids.update(
            order_id
            for (order_id,) in db.session.query(Order.id)
            .filter(func.lower(Order.customer_phone).in_(profile_phones))
            .all()
        )

    messages = []
    if available[EmailMessage]:
        message_filters = [
            EmailMessage.account_id == account.id,
            func.lower(EmailMessage.recipient) == account_email.lower(),
        ]
        if order_ids:
            message_filters.append(EmailMessage.order_id.in_(order_ids))
        messages = (
            EmailMessage.query.filter(or_(*message_filters))
            .with_for_update()
            .all()
        )
        if any(
            message.status == "sending"
            or (message.claim_token and message.status in {"queued", "sending"})
            for message in messages
        ):
            raise CustomerCleanupBlocked(
                "A message is being dispatched for this account. Wait for the provider attempt to finish, then retry."
            )
        if available[EmailDeliveryAttempt] and messages:
            in_flight = EmailDeliveryAttempt.query.filter(
                EmailDeliveryAttempt.message_id.in_([message.id for message in messages]),
                EmailDeliveryAttempt.status == "in_flight",
            ).first()
            if in_flight is not None:
                raise CustomerCleanupBlocked(
                    "A message is being dispatched for this account. Wait for the provider attempt to finish, then retry."
                )

    order_ids.update(message.order_id for message in messages if message.order_id is not None)
    if order_ids and available[Order]:
        orders = (
            Order.query.filter(Order.id.in_(order_ids))
            .with_for_update()
            .all()
        )
        order_ids = {order.id for order in orders}
    elif order_ids:
        raise CustomerCleanupBlocked(
            "Linked order storage is unavailable; permanent deletion was stopped."
        )
    else:
        orders = []

    identities.extend(
        order.customer_phone for order in orders if order.customer_phone
    )
    identities.extend(
        order.customer_name for order in orders
        if order.customer_name and order.customer_name != "Deleted customer"
    )
    proof_paths = []
    proof_filenames = {
        order.payment_proof_filename
        for order in orders
        if order.payment_proof_filename
    }
    if proof_filenames:
        shared_proof = Order.query.filter(
            Order.payment_proof_filename.in_(proof_filenames),
            ~Order.id.in_(order_ids),
        ).first()
        if shared_proof is not None:
            raise CustomerCleanupBlocked(
                "A payment proof is also referenced by another order; permanent deletion was stopped."
            )
        proof_folder_setting = current_app.config.get("PAYMENT_PROOF_FOLDER")
        if not proof_folder_setting:
            raise CustomerCleanupBlocked(
                "Payment proof storage is not configured; permanent deletion was stopped."
            )
        proof_folder = Path(proof_folder_setting).resolve()
        for order in orders:
            if not order.payment_proof_filename:
                continue
            filename = Path(order.payment_proof_filename)
            if filename.name != order.payment_proof_filename:
                raise CustomerCleanupBlocked(
                    "A payment proof has an unsafe filename; no account data was changed."
                )
            proof_path = (proof_folder / filename.name).resolve()
            if proof_path.parent != proof_folder:
                raise CustomerCleanupBlocked(
                    "A payment proof path is unsafe; no account data was changed."
                )
            proof_paths.append(proof_path)

    cart_ids = set()
    if available[CustomerCart]:
        cart_ids.update(
            row_id
            for (row_id,) in db.session.query(CustomerCart.id)
            .filter_by(account_id=account.id)
            .all()
        )

    drafts_to_delete = set()
    if available[EmailDraft]:
        for draft in EmailDraft.query.all():
            draft_content = " ".join(
                value or ""
                for value in (
                    draft.name,
                    draft.subject,
                    draft.headline,
                    draft.message,
                    draft.blocks_json,
                    draft.account_ids_json,
                )
            ).lower()
            has_identity = any(
                identity and identity.lower() in draft_content
                for identity in [account_email, *profile_phones]
            )
            try:
                recipient_ids = json.loads(draft.account_ids_json or "[]")
            except json.JSONDecodeError:
                if str(account.id) in (draft.account_ids_json or "") or has_identity:
                    raise CustomerCleanupBlocked(
                        "An email draft has an unreadable recipient list; review it before permanent deletion."
                    )
                continue
            if not isinstance(recipient_ids, list):
                raise CustomerCleanupBlocked(
                    "An email draft has an invalid recipient list; review it before permanent deletion."
                )
            if (
                any(str(value) == str(account.id) for value in recipient_ids)
                or draft.cart_id in cart_ids
                or draft.order_id in order_ids
                or has_identity
            ):
                drafts_to_delete.add(draft.id)

    referral_ids = set()
    if available[Referral]:
        referral_filters = []
        if profile_phones:
            referral_filters.extend((
                func.lower(Referral.referrer_key).in_(profile_phones),
                func.lower(Referral.referred_key).in_(profile_phones),
            ))
        if profile_codes:
            referral_filters.append(Referral.referral_code.in_(profile_codes))
        if order_ids:
            referral_filters.append(Referral.qualifying_order_id.in_(order_ids))
        if referral_filters:
            referral_ids.update(
                row_id
                for (row_id,) in db.session.query(Referral.id)
                .filter(or_(*referral_filters))
                .all()
            )

    loyalty_ids = set()
    if available[LoyaltyTransaction]:
        loyalty_filters = []
        if profile_phones:
            loyalty_filters.append(
                func.lower(LoyaltyTransaction.customer_key).in_(profile_phones)
            )
        if order_ids:
            loyalty_filters.append(LoyaltyTransaction.order_id.in_(order_ids))
        if loyalty_filters:
            loyalty_ids.update(
                row_id
                for (row_id,) in db.session.query(LoyaltyTransaction.id)
                .filter(or_(*loyalty_filters))
                .all()
            )

    roots = {"customer_account": [(account.id,)]}
    connection = db.session.connection()
    schema_inspector = inspect(connection)
    for table_name in schema_inspector.get_table_names():
        table_columns = {
            column["name"] for column in schema_inspector.get_columns(table_name)
        }
        account_columns = [
            column for column in ("account_id", "customer_account_id")
            if column in table_columns
        ]
        if not account_columns:
            continue
        table = Table(table_name, MetaData(), autoload_with=connection, resolve_fks=False)
        if not table.primary_key.columns:
            matching_row = any(
                connection.execute(
                    select(1).select_from(table)
                    .where(table.c[account_column] == account.id)
                    .limit(1)
                    .with_for_update()
                ).first()
                for account_column in account_columns
            )
            if matching_row:
                raise CustomerCleanupBlocked(
                    "A related database table has no primary key; permanent deletion was stopped."
                )
            continue
        for account_column in account_columns:
            rows = db.session.execute(
                select(*table.primary_key.columns)
                .where(table.c[account_column] == account.id)
                .with_for_update()
            ).all()
            roots.setdefault(table_name, []).extend(tuple(row) for row in rows)
    for message in messages:
        roots.setdefault("email_message", []).append((message.id,))
    for order_id in order_ids:
        roots.setdefault("order", []).append((order_id,))
    for table_name, ids in (
        ("product_enquiry", ()),
        ("restock_alert", ()),
        ("email_draft", drafts_to_delete),
        ("referral", referral_ids),
        ("loyalty_transaction", loyalty_ids),
    ):
        roots.setdefault(table_name, []).extend((row_id,) for row_id in ids)

    contact_identities = {account_email.lower(), *profile_phones}
    for model in (ProductEnquiry, RestockAlert):
        if available[model]:
            if contact_identities:
                rows = model.query.filter(
                    func.lower(model.contact).in_(contact_identities)
                ).all()
                roots.setdefault(model.__tablename__, []).extend(
                    (row.id,) for row in rows
                )

    if available[ProductSuggestion]:
        terms = {account_email, *profile_phones}
        suggestion_filters = [
            getattr(ProductSuggestion, column).ilike(f"%{term}%")
            for term in terms
            for column in ("product_name", "category", "preferences", "price_range", "details")
        ]
        if suggestion_filters:
            suggestion_ids = db.session.query(ProductSuggestion.id).filter(
                or_(*suggestion_filters)
            ).all()
            roots.setdefault("product_suggestion", []).extend(
                (row_id,) for (row_id,) in suggestion_ids
            )

    if available[Order] and (profile_phones or profile_codes):
        referral_order_filters = []
        if profile_phones:
            referral_order_filters.append(func.lower(Order.referrer_key).in_(profile_phones))
        if profile_codes:
            referral_order_filters.append(Order.referral_code.in_(profile_codes))
        referred_orders = Order.query.filter(or_(*referral_order_filters)).all()
        for order in referred_orders:
            if order.id not in order_ids:
                order.referrer_key = ""
                order.referral_code = ""
                order.customer_name = _redact_text(order.customer_name, identities)
                order.instructions = _redact_text(order.instructions, identities)
                order.notes = _redact_text(order.notes, identities)

    if available[CustomerProfile] and profile_codes:
        for profile in CustomerProfile.query.filter(
            CustomerProfile.referred_by_code.in_(profile_codes),
            CustomerProfile.account_id != account.id,
        ).all():
            profile.referred_by_code = ""

    audit_filters = [
        (FinancialAuditEvent.entity_type == "CustomerAccount")
        & (FinancialAuditEvent.entity_id == account.id)
    ]
    if order_ids:
        audit_filters.append(
            (FinancialAuditEvent.entity_type == "Order")
            & FinancialAuditEvent.entity_id.in_(order_ids)
        )
    audit_rows = FinancialAuditEvent.query.filter(or_(*audit_filters)).all()
    roots.setdefault("financial_audit_event", []).extend(
        (event.id,) for event in audit_rows
    )
    if identities:
        for event in FinancialAuditEvent.query.all():
            event.note = _redact_text(event.note, identities)

    for message in messages:
        if message.account_id not in (None, account.id):
            raise CustomerCleanupBlocked(
                "An email record is linked to a different customer account; permanent deletion was stopped."
            )
        message.account_id = account.id

    account.email = f"removed-customer-{account.id}@redacted.invalid"
    account.display_name = "Deleted customer"
    account.password_hash = None
    account.google_sub = None
    account.email_verified_at = None
    account.two_factor_enabled = False
    account.marketing_status = "unsubscribed"
    account.marketing_consent_at = None
    account.marketing_withdrawn_at = None
    account.active = False
    db.session.flush()
    if db.session.get_bind().dialect.name == "postgresql":
        db.session.execute(
            text("SELECT set_config('fity.customer_erasure_account_id', :account_id, true)"),
            {"account_id": str(account.id)},
        )

    deleted_counts = _permanent_delete_fk_rows(roots)
    db.session.expire_all()
    db.session.add(FinancialAuditEvent(
        admin_id=admin_id,
        event_type="customer_account_permanently_deleted",
        entity_type="CustomerAccount",
        entity_id=None,
        note=json.dumps({
            "deleted_row_counts": deleted_counts,
            "payment_proof_files": len(set(proof_paths)),
        }, sort_keys=True),
    ))
    return {"counts": deleted_counts, "proof_paths": list(set(proof_paths))}
