# FityStores Email Studio guide

Email Studio lets an admin build, preview, save, and manually send customer emails. Saving or previewing never sends a message. Email is sent only after you use one of the explicit send actions and complete its confirmation.

## Build and preview an email

1. Open **Admin → Customer email → Open Email Studio**.
2. Choose an email purpose. Promotional messages require an active customer who opted in. Service and transactional messages should contain only the necessary account, order, or service information. Promotional wording in a service message can trigger marketing safeguards.
3. Choose a saved template, if you want to start from one. Templates are scoped to their email purpose. You can change the subject, headline, and content before saving or sending.
4. Load a customer context. The preview uses that customer's information. For order confirmations, order updates, and other order-specific messages, also choose one of that customer's linked orders.
5. Add blocks using **Add a content block → Add block**. Edit the new block in the canvas. Use the up/down controls to reorder blocks and **Remove** to delete a block. Product collections and image banners use the catalogue selections; choose the products needed for those blocks.
   - For cart reminders, product collections, banners, and product buttons automatically use the loaded customer's current cart items. Each recipient's own cart is used when the reminder is sent.
6. On desktop, the preview stays in the right-side panel while you scroll. It refreshes as you edit. Use **Desktop preview** or **Mobile preview** to change its width. On a smaller screen, the preview appears in the page flow below the editor.
7. Review the subject, rendered email, and plain-text version. If the preview reports a missing customer, order, or product choice, correct that context and it will refresh.

## Save work for later or reuse it

- **Save template** updates the selected template for this email purpose. If there is no selected saved template, it saves the category's reusable template.
- **Save as new template** creates another named template without replacing the selected one. Give it a distinctive name first.
- **Duplicate template** creates a separate copy from the current content; provide a name for the copy.
- **Save draft** stores the current message, blocks, product choices, selected recipients, and preview-customer context for later editing. Drafts are private to the admin who created them.
- To reuse a full visual template, open it from the **Saved template** selector in Email Studio.
- In **Customer email**, the promotional and service template selectors load the saved template's subject and message into the corresponding quick-send form. That form sends its own simple branded email; use Email Studio when you need the saved visual blocks and layout.

## Send a test or a real email

1. Preview the exact content and context you plan to send.
2. For a test, enter an address you control, type **SEND TEST**, and choose **Send test email**. Tests are labelled and are not sent to the customer list.
3. For a real send, select the intended recipients in the relevant recipient list. Marketing recipients must be active and opted in; the server checks consent and unsubscribe status again when sending. Service messages go only to active, verified accounts and must not contain promotions.
4. Enter the exact confirmation shown, such as **SEND 2**, then choose **Send manually**. A preview is required for the exact content and recipient selection before the send is accepted.
5. Check **Customer email → Recent email history** for provider status. “Provider accepted” means the email service accepted the request; it does not guarantee inbox delivery.

Cart reminders can be previewed at any time, but sending is held while checkout is active, until the cart has been idle for two hours, and after a reminder has already been sent for that cart episode.

For a general promotional or service message that does not need visual blocks, use the matching form in **Customer email**. The Email Studio category picker is preferable for designed emails, order-specific messages, and saved visual templates.
