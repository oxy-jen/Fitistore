# FityStore email template inventory

This inventory reflects the implementation in the current local tree. “Available” means a code-backed template or composer purpose exists; it does not mean an automation is scheduled or enabled. All campaign sends remain subject to the centralized eligibility, suppression, unsubscribe and cooldown checks. Marketing dispatch remains paused. Automatic draft preparation is separate and default-off. No real email was sent for this inventory work.

## Email Studio purposes

| Purpose | Studio key | Delivery classification | Template/layout | Trigger or usage | Status |
| --- | --- | --- | --- | --- | --- |
| Cart reminder | `cart_reminder` | Marketing | Branded Studio cart layout with the recipient’s current cart | Admin manual send only; cart episode and eligibility rechecked | Available; automation paused |
| Promotions and new arrivals | `promotion` | Marketing | `emails/promotion_message.html` | Admin compose, preview, confirm, or schedule | Available |
| Daily Fity Goodies | `daily_goodies` | Marketing | Promotional campaign layout | Manual campaign purpose; no schedule runs | Available; automation paused |
| Weekly product picks | `weekly_picks` | Marketing | Promotional campaign layout | Admin-authored or idempotent weekly draft preparation; administrator must preview and schedule | Available; preparation default-off; dispatch separately gated |
| New product announcement | `new_product_announcement` | Marketing | Promotional campaign layout | Admin-authored or draft prepared for newly listed/restocked available products; administrator must edit/preview/schedule | Available; preparation default-off; dispatch separately gated |
| Special offers | `special_offers` | Marketing | Promotional campaign layout | Manual campaign purpose; offer content must be authored/verified by admin | Available; no offer trigger automation |
| Back-in-stock announcement | `back_in_stock` | Marketing | Promotional campaign layout | Manual campaign purpose; broad promotional announcement | Available; automated personal restock notices are separate service messages |
| Marketing welcome | `marketing_welcome` | Marketing | Promotional campaign layout | Manual campaign purpose; distinct from account-registration welcome | Available; no automatic welcome-campaign trigger |
| Re-engagement campaign | `re_engagement` | Marketing | Promotional campaign layout | Manual campaign purpose | Available; no automatic re-engagement draft |
| Custom promotional campaign | `custom_campaign` | Marketing | Promotional campaign layout | Manual campaign purpose | Available |
| Custom customer-service email | `customer_service` | Marketing by default | Promotional layout for editable content | Manual, custom-authored; linked order context is available but does not grant transactional status | Available; structured support-case reply is not implemented |
| Order confirmation | `order_confirmation` | Transactional only when unchanged server-defined content | Constrained Studio layout | Admin manually sends against a linked order | Available in constrained form; no automatic Studio trigger |
| Thank-you email | `thank_you` | Transactional only when unchanged server-defined content | Constrained Studio layout | Admin manually sends against a linked order | Available in constrained form |
| Order status / fulfilment update | `order_status` | Transactional only when unchanged server-defined content | Constrained Studio layout; automatic system event uses branded base template | Existing order status transition and constrained Studio | Available |

The selector groups campaign, transactional/order-service and customer-service purposes. Arbitrary edits to the three order-notice categories are classified as marketing. The submitted category is not the delivery authority: `app/services/email_purpose.py` maps each Studio campaign purpose to a server-known email kind.

Campaign purposes use the existing BEI FITS promotional layout and catalogue imagery. Cart reminders keep their specialized cart summary layout so that live cart quantities and totals remain accurate. Transactional/account notices retain branded Fity layouts without promotional product or offer copy.

## Required system-template comparison

| Required email | Existing backend/template | Trigger found | Studio visibility | Correct purpose | Status / remaining work |
| --- | --- | --- | --- | --- | --- |
| Email verification | `_send_token_email()` / `emails/base_branded.html` | Registration/resend | System inventory and sample preview only | Transactional | Implemented |
| Registration welcome | `customer.verify_email()` / base branded | Successful verification | System inventory and sample preview only | Transactional | Implemented |
| Login verification code | `_start_email_code()` / base branded | Customer requests code | System inventory and sample preview only | Transactional | Implemented |
| Password reset | `_send_token_email()` / base branded | Customer request | System inventory and sample preview only | Transactional | Implemented |
| Password changed notice | `queue_branded_email()` / base branded | Successful password change | System inventory and sample preview only | Transactional | Implemented |
| Security alert | Generic security-notice rendering exists | No distinct alert event found | Listed as system-managed, unavailable | Transactional | Add a defined security event and idempotent trigger before enabling |
| Email address change verification | No matching workflow found | None | Listed as unavailable | Transactional | Missing |
| Account deactivation confirmation | No matching workflow found | None | Listed as unavailable | Transactional | Missing |
| Marketing consent confirmation | `_send_token_email()` / base branded | Customer requests subscription confirmation | System inventory and sample preview only | Transactional preference confirmation | Implemented; this message does not itself subscribe the account |
| Unsubscribe confirmation | Customer unsubscribe handler / base branded | Successful preference update | System inventory and sample preview only | Transactional preference confirmation | Implemented |
| Order confirmation | `notify_order_event('order_received')` / `emails/order_received.html` | Order creation with linked active verified customer | Inventory and sample preview | Transactional | Implemented |
| Payment confirmation | `notify_order_event('payment_confirmed')` / base branded | Verified payment state is `Paid` | Inventory and sample preview | Transactional | Implemented |
| Payment failure status notice | `notify_order_event('payment_failed')` / base branded | Authenticated admin changes a linked order payment status to `Failed` | Inventory and sample preview | Transactional status notice; it describes the status recorded by the store, not a provider-confirmed payment attempt | Implemented and idempotent by payment-status history event; no provider webhook failure source is present |
| Requested item restock notice | `notify_restock_alert()` / base branded | Inventory increase makes a product available for a pending email restock request with explicit contact consent | Inventory and sample preview | Transactional/service notice limited to the requested item; marketing consent is not required | Implemented via the central outbox when its schema is present; no external provider send occurs in inventory adjustment |
| Order processing | Generic `order_status` exists | Status transition to preparation | Inventory notes generic coverage | Transactional | Separate purpose-specific template/trigger not implemented |
| Ready for collection | Generic `order_status` has status copy | Status transition | Inventory notes generic coverage | Transactional | Dedicated template/trigger not implemented |
| Order dispatched | No distinct dispatch event found | None | Listed as unavailable | Transactional | Missing |
| Order delivered | No confirmed delivery event found | None | Listed as unavailable | Transactional | Missing |
| Order cancellation | Generic `order_status` has cancellation copy | Cancellation status transition | Inventory notes generic coverage | Transactional | Dedicated template/trigger not implemented |
| Refund confirmation | `notify_refund_completed()` after an admin records completion with a verified reference | Static system preview; completion notification uses the verified linked account | Transactional | Implemented; idempotent outbox enqueue after committed completion |
| Order status / fulfilment update | `notify_order_event('order_status')` / base branded | Existing status transition | Studio purpose and system inventory | Transactional | Implemented |
| Thank-you email | Constrained `thank_you` Studio defaults | Admin-initiated, linked order | Studio purpose | Transactional only when unedited | Available as constrained manual template; no automatic trigger |
| Order review request | `notify_order_event('review_request')` / base branded | Order is `Completed` and `Paid` | Inventory and sample preview | Transactional feedback about that order | Implemented; no promotions are included |

## Customer-service and operational requirements

| Required template | Current finding | Studio visibility | Status |
| --- | --- | --- | --- |
| Structured reply linked to genuine support case/order | Free-form customer-service messages are marketing by default. No support-case model/structured reply trigger was found. | Custom service composer is visible; system inventory marks structured replies unavailable | Structured service workflow missing |
| Critical email delivery failure | Delivery investigation records exist; no admin alert sender found | Operational inventory, administrator-only label | Missing |
| Queue health alert | Queue/worker tracking exists; no alert delivery found | Operational inventory, administrator-only label | Missing |
| Worker failure | Worker run diagnostics exist; no alert delivery found | Operational inventory, administrator-only label | Missing |
| Automation execution report | Marketing automation is a paused no-op | Operational inventory, administrator-only label | Missing; do not enable scheduler |
| Campaign completion report | No report delivery found | Operational inventory, administrator-only label | Missing |
| Significant suppression event | Suppression audit records exist; no alert trigger found | Operational inventory, administrator-only label | Missing |

Operational and system-managed definitions cannot be selected as unrestricted customer sends. The sample-preview route renders fixed example data only; it does not issue tokens, mutate account preferences, enqueue mail, or call the provider. Unimplemented inventory entries have no preview and cannot be queued.

## Validation and coverage

- Variable substitution is allow-listed per Studio category in `app/services/email_studio.py`.
- Unknown categories and email kinds fail closed in `app/services/email_purpose.py`.
- Marketing category eligibility is re-evaluated in Email Studio and again by the shared queue/dispatch path.
- `admin_test` is classified as test-only. The additive `20261117_email_admin_test_purpose` migration corrects historical outbox purpose values without changing the checksum of the original outbox migration.
- Saved Email Studio templates remain in their existing tables. Automatic campaign draft idempotency metadata uses the separate additive `20261121_marketing_campaign_draft_preparation` migration; it has not been run.
- Marketing dispatch remains paused. The 15-minute workflow can prepare new-arrival, abandoned-cart, and weekly-picks drafts only when the separate `EMAIL_MARKETING_PREPARATION_ENABLED` gate is explicitly enabled; the gate defaults off. Drafts still need administrator editing, preview, and scheduling. Daily Goodies remains a manual campaign purpose.

## Implementation references

- `app/services/email_studio.py`: purpose definitions, validated rendering and Fity campaign layout selection.
- `app/services/email_purpose.py`: server-side Studio category to queue-kind mapping.
- `app/services/email_template_inventory.py`: grouped code-backed inventory and implementation status.
- `app/admin/email_studio.py`: composer, previews, manual-send validation and system sample preview.
- `app/templates/admin/email_studio.html`: purpose selector, recipient controls and composer.
- `app/templates/admin/studio_messages.html`: saved templates, drafts and template library.
- `app/templates/admin/email_template_preview.html`: no-send sample view for supported system templates.
