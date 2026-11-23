"""Global test isolation for email-provider network calls.

Individual adapter tests may override urlopen to exercise success/error paths.
All other tests receive a deterministic provider acceptance response and can
never contact a live email service.
"""
import pytest


@pytest.fixture(autouse=True)
def block_live_email_provider_requests(monkeypatch):
    from app.services import customer_mail

    class FakeProviderResponse:
        status = 201

        def read(self):
            return b'{"id":"test-provider-message-id","messageId":"test-provider-message-id"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        customer_mail.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeProviderResponse(),
    )
