# FityStores email system inventory

The application sends mail synchronously through the configured provider. Brevo requests are made per recipient; delivery records retain provider status and a safe failure reason. Email Studio and administrator sends require an authenticated admin and CSRF token. Promotional sends also check active subscription and unsubscribe state.

## Automatic and account mail

| Purpose | Trigger | Template |
| --- | --- | --- |
| Order received | Existing order creation notification, for an active account with verified email | `emails/order_received.html` |
| Payment confirmed | Payment event, only when stored payment status is `Paid` | Shared `emails/base_branded.html` |
| Order status update | Existing order status event, active account with verified email | Shared `emails/base_branded.html` |
| Email verification / password reset / marketing preference link | Customer requests the corresponding one time token | Shared `emails/base_branded.html` |
| Verification / sign in code | Customer requests a one time code; expiry remains the existing ten minutes | Shared `emails/base_branded.html` |
| Welcome / password changed / unsubscribe confirmation | Existing account lifecycle event | Shared `emails/base_branded.html` |
| Review request | Existing completed and paid order eligibility event | Shared `emails/base_branded.html` |

## Administrator and campaign mail

| Purpose | Trigger | Template |
| --- | --- | --- |
| Order confirmation, thank you, order status, cart reminder, promotion, customer service | Admin selects a category, previews, then confirms manual send | `emails/studio_message.html` |
| Campaign product recommendations and abandoned cart | Existing marketing flow, subject to consent and suppression checks | `emails/product_campaign.html` |
| Admin customer campaign | Admin selects recipients and submits from customer email centre | `emails/manual_customer_email.html` |
| Test message | Admin previews and explicitly confirms an address they control | `emails/studio_message.html` with a test banner |

Saving a Studio draft or template does not send mail or schedule a future send. Saved design data remains in the existing template/draft JSON fields; no schema migration is needed.

## Design and sending notes

`emails/_brand_components.html` provides the common FITY header/footer. Auth, account, status, Studio, and manual customer emails use those shared components. Studio's validated JSON design block stores colors, preheader, footer note, and image presentation settings alongside existing content blocks. Email Studio renders on the server for both its live preview and the final send.

The Studio preview and confirmation fingerprint must match the selected customer/order, audience, products, content, and design. A repeated operation key is idempotent: previously accepted messages are reported separately and are not resent. Provider acceptance means the provider accepted the request; it does not prove inbox delivery.
