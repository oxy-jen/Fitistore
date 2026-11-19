import json
import re
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from flask import current_app
from sqlalchemy import func, inspect, or_

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
        if order.payment_proof_filename and (delete_orders or delete_payment_proofs):
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

    if delete_orders and order_ids:
        if available[EmailMessage]:
            db.session.query(EmailMessage).filter(EmailMessage.order_id.in_(order_ids)).update(
                {EmailMessage.order_id: None}, synchronize_session=False
            )
        if available[EmailDraft]:
            db.session.query(EmailDraft).filter(EmailDraft.order_id.in_(order_ids)).update(
                {EmailDraft.order_id: None}, synchronize_session=False
            )
        if available[Referral]:
            db.session.query(Referral).filter(Referral.qualifying_order_id.in_(order_ids)).update(
                {Referral.qualifying_order_id: None}, synchronize_session=False
            )
        if available[LoyaltyTransaction]:
            db.session.query(LoyaltyTransaction).filter(LoyaltyTransaction.order_id.in_(order_ids)).update(
                {LoyaltyTransaction.order_id: None}, synchronize_session=False
            )
        attempt_ids = []
        if available[PaymentAttempt]:
            attempt_ids = [
                row.id
                for row in db.session.query(PaymentAttempt.id)
                .filter(PaymentAttempt.order_id.in_(order_ids))
                .all()
            ]
        if available[RefundRequest]:
            db.session.query(RefundRequest).filter(
                RefundRequest.order_id.in_(order_ids)
            ).delete(synchronize_session=False)
        if available[PaymentTransaction]:
            payment_filter = PaymentTransaction.order_id.in_(order_ids)
            if attempt_ids:
                payment_filter = or_(
                    payment_filter,
                    PaymentTransaction.payment_attempt_id.in_(attempt_ids),
                )
            db.session.query(PaymentTransaction).filter(payment_filter).delete(
                synchronize_session=False
            )
        for model in (PaymentAttempt, OrderStatusHistory, OrderItem):
            if available[model]:
                db.session.query(model).filter(model.order_id.in_(order_ids)).delete(synchronize_session=False)
        if available[Order]:
            counts["orders"] = db.session.query(Order).filter(Order.id.in_(order_ids)).delete(
                synchronize_session=False
            )
    else:
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

    if available[CustomerOrderLink]:
        counts["order_links"] = db.session.query(CustomerOrderLink).filter_by(
            account_id=account.id
        ).delete(synchronize_session=False)

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
            "delete_orders": bool(delete_orders),
            "delete_payment_proofs": bool(delete_payment_proofs or delete_orders),
            "delete_reviews": bool(delete_reviews),
            "delete_referral_history": bool(delete_referral_history),
            "delete_product_exposure": bool(delete_product_exposure),
            "delete_email_drafts": bool(delete_email_drafts),
            "counts": counts,
        }, sort_keys=True),
    ))
    return {"counts": counts, "proof_paths": proof_paths}
