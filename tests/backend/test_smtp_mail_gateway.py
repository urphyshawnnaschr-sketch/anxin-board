from __future__ import annotations

from pathlib import Path
import smtplib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.mail_gateway import (  # noqa: E402
    MailGatewayCallError,
    MailMessage,
    RecipientOutcome,
)
from app.secret_store import InMemorySecretStore  # noqa: E402
from app.smtp_mail_gateway import (  # noqa: E402
    SmtpGatewayConfig,
    SmtpSecurity,
    StdlibSmtpMailGateway,
)


class _FakeSmtpClient:
    def __init__(
        self,
        *,
        send_result=None,
        send_error: BaseException | None = None,
        login_error: BaseException | None = None,
    ) -> None:
        self.events: list[str] = []
        self.send_result = {} if send_result is None else send_result
        self.send_error = send_error
        self.login_error = login_error
        self.sent_message = None
        self.from_addr = None
        self.to_addrs = None
        self.login_user = None
        self.login_secret = None

    def ehlo(self):
        self.events.append("ehlo")
        return 250, b"ok"

    def starttls(self, *, context):
        assert context is not None
        self.events.append("starttls")
        return 220, b"ready"

    def login(self, user: str, password: str):
        self.events.append("login")
        self.login_user = user
        self.login_secret = password
        if self.login_error is not None:
            raise self.login_error
        return 235, b"ok"

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.events.append("send_message")
        self.sent_message = msg
        self.from_addr = from_addr
        self.to_addrs = tuple(to_addrs or ())
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    def quit(self):
        self.events.append("quit")
        return 221, b"bye"

    def close(self):
        self.events.append("close")


def _config(*, security: SmtpSecurity = SmtpSecurity.STARTTLS) -> SmtpGatewayConfig:
    return SmtpGatewayConfig(
        host="smtp.example.test",
        port=587 if security is SmtpSecurity.STARTTLS else 465,
        security=security,
        username="sender@example.test",
        secret_ref="mail.smtp.password",
        timeout_seconds=30,
    )


def _message() -> MailMessage:
    return MailMessage(
        message_id="<report-19@example.test>",
        from_identity="reports@example.test",
        to_recipients=("a@example.test", "b@example.test"),
        subject="Daily report",
        html_body="<html><body>daily</body></html>",
        attempt_correlation_id="send-attempt-1",
    )


def _store() -> InMemorySecretStore:
    store = InMemorySecretStore()
    store.put("mail.smtp.password", "unit-test-secret")
    return store


def test_s01_starttls_auth_and_success_are_translated_without_network():
    client = _FakeSmtpClient()
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert client.events == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert client.login_user == "sender@example.test"
    assert client.login_secret == "unit-test-secret"
    assert client.from_addr == "reports@example.test"
    assert client.to_addrs == ("a@example.test", "b@example.test")
    assert client.sent_message["Message-ID"] == "<report-19@example.test>"
    assert tuple(item.outcome for item in result.recipient_results) == (
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.ACCEPTED,
    )


def test_s02_returned_recipient_refusal_maps_only_exact_recipient_to_rejected():
    client = _FakeSmtpClient(send_result={"b@example.test": (550, b"no")})
    gateway = StdlibSmtpMailGateway(
        config=_config(security=SmtpSecurity.IMPLICIT_TLS),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert client.events == ["login", "send_message", "quit"]
    assert tuple(item.outcome for item in result.recipient_results) == (
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.REJECTED,
    )
    assert result.recipient_results[1].error_code == "MAIL_SMTP_RECIPIENT_REJECTED"


def test_s03_all_recipient_refusal_exception_preserves_explicit_rejected_evidence():
    refused = {
        "a@example.test": (550, b"no"),
        "b@example.test": (550, b"no"),
    }
    client = _FakeSmtpClient(send_error=smtplib.SMTPRecipientsRefused(refused))
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert all(item.outcome is RecipientOutcome.REJECTED for item in result.recipient_results)
    assert all(item.error_code == "MAIL_SMTP_RECIPIENT_REJECTED" for item in result.recipient_results)


def test_s04_post_submission_timeout_is_unknown_for_every_unproven_recipient():
    client = _FakeSmtpClient(send_error=TimeoutError("response lost"))
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert all(item.outcome is RecipientOutcome.UNKNOWN for item in result.recipient_results)
    assert all(item.error_code == "MAIL_SMTP_OUTCOME_UNKNOWN" for item in result.recipient_results)


def test_s05_missing_secret_is_definite_pre_submit_failure_and_opens_no_client():
    opened: list[object] = []
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=InMemorySecretStore(),
        client_factory=lambda config: opened.append(config),
    )

    with pytest.raises(MailGatewayCallError) as caught:
        gateway.send(_message())

    assert caught.value.code == "MAIL_SMTP_SECRET_UNAVAILABLE"
    assert opened == []
    assert "unit-test-secret" not in str(caught.value)


def test_s06_sender_refusal_is_definite_pre_submission_failure():
    client = _FakeSmtpClient(
        send_error=smtplib.SMTPSenderRefused(550, b"sender refused", "reports@example.test")
    )
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    with pytest.raises(MailGatewayCallError) as caught:
        gateway.send(_message())

    assert caught.value.code == "MAIL_SMTP_SENDER_REJECTED"


def test_s07_explicit_data_rejection_maps_message_to_rejected_not_unknown():
    client = _FakeSmtpClient(send_error=smtplib.SMTPDataError(554, b"message rejected"))
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert all(item.outcome is RecipientOutcome.REJECTED for item in result.recipient_results)
    assert all(item.error_code == "MAIL_SMTP_MESSAGE_REJECTED" for item in result.recipient_results)


def test_s08_unknown_returned_refusal_shape_fails_closed_to_unknown():
    client = _FakeSmtpClient(send_result={"not-requested@example.test": (550, b"no")})
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert all(item.outcome is RecipientOutcome.UNKNOWN for item in result.recipient_results)


def test_s09_malformed_all_refused_exception_cannot_manufacture_acceptance():
    client = _FakeSmtpClient(
        send_error=smtplib.SMTPRecipientsRefused({"b@example.test": (550, b"no")})
    )
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    result = gateway.send(_message())

    assert all(item.outcome is RecipientOutcome.UNKNOWN for item in result.recipient_results)


def test_s10_unclassified_login_failure_is_still_definite_pre_submit():
    client = _FakeSmtpClient(login_error=RuntimeError("test handshake failure"))
    gateway = StdlibSmtpMailGateway(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda _config: client,
    )

    with pytest.raises(MailGatewayCallError) as caught:
        gateway.send(_message())

    assert caught.value.code == "MAIL_SMTP_PRE_SUBMIT_FAILED"
    assert "send_message" not in client.events


def test_s11_smtp_username_rejects_unicode_line_separator():
    with pytest.raises(ValueError) as caught:
        SmtpGatewayConfig(
            host="smtp.example.test",
            port=587,
            security=SmtpSecurity.STARTTLS,
            username="sender\u2028@example.test",
            secret_ref="mail.smtp.password",
        )

    assert str(caught.value) == "MAIL_SMTP_USERNAME_INVALID"


def test_fixed_brand_png_preserves_original_alpha_and_inverts_pixels():
    from app.mail_brand_asset import get_calligraphy_png, CALLIGRAPHY_SHA256
    import hashlib, struct, zlib
    def pixels(raw):
        width,height,depth,color=struct.unpack('>IIBB',raw[16:26])
        assert depth==8 and color==6
        compressed=b'';pos=8
        while pos<len(raw):
            size=struct.unpack('>I',raw[pos:pos+4])[0]
            if raw[pos+4:pos+8]==b'IDAT':compressed+=raw[pos+8:pos+8+size]
            pos+=12+size
        data=zlib.decompress(compressed);stride=width*4;previous=bytearray(stride);result=bytearray();pos=0
        for _ in range(height):
            kind=data[pos];pos+=1;row=bytearray(data[pos:pos+stride]);pos+=stride
            for i in range(stride):
                left=row[i-4] if i>=4 else 0;above=previous[i];upper=previous[i-4] if i>=4 else 0
                if kind==0:predict=0
                elif kind==1:predict=left
                elif kind==2:predict=above
                elif kind==3:predict=(left+above)//2
                elif kind==4:
                    p=left+above-upper;dist=[abs(p-left),abs(p-above),abs(p-upper)]
                    predict=[left,above,upper][dist.index(min(dist))]
                else:raise AssertionError('PNG filter')
                row[i]=(row[i]+predict)&255
            result.extend(row);previous=row
        return (width,height),bytes(result)
    original=(Path(__file__).resolve().parents[2]/'apps/frontend/src/assets/anxin-board-calligraphy.png').read_bytes()
    raw=get_calligraphy_png()
    assert hashlib.sha256(raw).hexdigest()==CALLIGRAPHY_SHA256
    size,actual=pixels(raw);oldsize,old=pixels(original)
    assert size==oldsize
    assert actual[3::4]==old[3::4]
    for channel in range(3):assert actual[channel::4]==bytes(255-v for v in old[channel::4])



@pytest.mark.parametrize('version', ['v4', 'v5', 'v6'])
def test_fixed_brand_related_mime_and_legacy_plain_html(version):
    from dataclasses import replace
    from app.mail_brand_asset import CALLIGRAPHY_CID,get_calligraphy_png
    from app.smtp_mail_gateway import _materialize_email
    legacy=_materialize_email(_message())
    assert legacy.get_content_type()=='text/html' and not legacy.is_multipart()
    html=f'<meta name="anxin-mail-render" content="{version}"><img src="cid:{CALLIGRAPHY_CID}">'
    mime=_materialize_email(replace(_message(),html_body=html))
    assert mime.get_content_type()=='multipart/related'
    parts=list(mime.iter_parts()); assert len(parts)==2
    assert parts[0].get_content().rstrip('\n')==html
    assert parts[1].get_content_type()=='image/png'
    assert parts[1]['Content-ID']==f'<{CALLIGRAPHY_CID}>'
    assert parts[1].get_content_disposition()=='inline'
    assert parts[1].get_payload(decode=True)==get_calligraphy_png()


@pytest.mark.parametrize('html',[
    '<meta name="anxin-mail-render" content="v4">',
    '<meta name="anxin-mail-render" content="v7"><img src="cid:unknown-digest">',
    '<meta name="anxin-mail-render" content="v7"><img src="file:///private-secret">',
    '<meta name="anxin-mail-render" content="v8"><img src="cid:unknown-digest">',
    '<meta name="anxin-mail-render" content="v8"><img src="https://example.invalid/remote.png">',
    '<meta name="anxin-mail-render" content="v9"><img src="cid:unknown-digest">',
    '<meta name="anxin-mail-render" content="v9"><img src="https://example.invalid/remote.png">',
    '<meta name="anxin-mail-render" content="v10"><img src="cid:unknown-digest">',
    '<meta name="anxin-mail-render" content="v10"><img src="https://example.invalid/remote.png">',
    '<meta name="anxin-mail-render" content="v11"><img src="cid:unknown-digest">',
    '<meta name="anxin-mail-render" content="v11"><img src="https://example.invalid/remote.png">',
    '<img src="cid:private-secret/path">',
    '<meta name="anxin-mail-render" content="v4"><img src="file:///private-secret">',
])
def test_invalid_brand_fails_before_credential_or_smtp(html):
    from dataclasses import replace
    class NoSecretStore(InMemorySecretStore):
        def get(self,*args):pytest.fail('credential must not be read')
    gateway=StdlibSmtpMailGateway(config=_config(),secret_store=NoSecretStore(),
        client_factory=lambda *a:pytest.fail('SMTP must not open'))
    with pytest.raises(MailGatewayCallError) as exc:gateway.send(replace(_message(),html_body=html))
    assert exc.value.code=='MAIL_SMTP_INLINE_ASSET_INVALID'
    assert 'private-secret' not in str(exc.value)


def test_duplicate_cid_and_asset_tampering_rejected():
    import app.mail_brand_asset as asset
    html=f'<meta name="anxin-mail-render" content="v4"><img src="cid:{asset.CALLIGRAPHY_CID}">'
    assert len(asset.validate_inline_assets(html))==1
    with pytest.raises(ValueError):asset.validate_inline_assets(html+f'<img src="cid:{asset.CALLIGRAPHY_CID}">')
    from unittest.mock import patch
    with patch.object(asset,'_PNG_BASE64','bm90LXBuZw=='):
        with pytest.raises(ValueError):asset.validate_inline_assets(html)


@pytest.mark.parametrize('variant',['unknown_digest','missing_marker','wrong_version','duplicate_marker','duplicate_src','wrong_tag'])
def test_brand_marker_and_cid_cannot_select_other_assets(variant):
    from app.mail_brand_asset import CALLIGRAPHY_CID,validate_inline_assets
    marker='<meta name="anxin-mail-render" content="v4">'
    image=f'<img src="cid:{CALLIGRAPHY_CID}">'
    cases={
        'unknown_digest':marker+'<img src="cid:anxin-calligraphy-'+('0'*64)+'@anxinboard.local">',
        'missing_marker':image,
        'wrong_version':marker.replace('v4','v99')+image,
        'duplicate_marker':marker+marker+image,
        'duplicate_src':marker+f'<img src="cid:{CALLIGRAPHY_CID}" src="file:///private">',
        'wrong_tag':marker+f'<a href="cid:{CALLIGRAPHY_CID}">x</a>',
    }
    with pytest.raises(ValueError,match='^MAIL_INLINE_ASSET_INVALID$'):validate_inline_assets(cases[variant])


def test_tampered_embedded_png_rejected_before_secret(monkeypatch):
    import app.mail_brand_asset as asset
    from dataclasses import replace
    class NoSecretStore(InMemorySecretStore):
        def get(self,*args):pytest.fail('credential must not be read')
    monkeypatch.setattr(asset,'_PNG_BASE64','bm90LXBuZw==')
    html=f'<meta name="anxin-mail-render" content="v4"><img src="cid:{asset.CALLIGRAPHY_CID}">'
    gateway=StdlibSmtpMailGateway(config=_config(),secret_store=NoSecretStore(),
        client_factory=lambda *a:pytest.fail('SMTP must not open'))
    with pytest.raises(MailGatewayCallError) as exc:gateway.send(replace(_message(),html_body=html))
    assert exc.value.code=='MAIL_SMTP_INLINE_ASSET_INVALID'


@pytest.mark.parametrize('version', ['v7', 'v8', 'v9', 'v10', 'v11'])
def test_customer_mail_and_self_contained_attachment_share_exact_template(version):
    import base64
    from dataclasses import replace
    from app.mail_brand_asset import CALLIGRAPHY_CID, get_calligraphy_png
    from app.smtp_mail_gateway import _materialize_email
    html = f'<meta name="anxin-mail-render" content="{version}"><div style="background:#102a56">客户正文<img src="cid:{CALLIGRAPHY_CID}"></div>'
    mime = _materialize_email(replace(_message(), html_body=html))
    assert mime.get_content_type() == 'multipart/mixed'
    related, attachment = list(mime.iter_parts())
    assert related.get_content_type() == 'multipart/related'
    body, image = list(related.iter_parts())
    assert body.get_content().rstrip('\n') == html
    assert image.get_payload(decode=True) == get_calligraphy_png()
    assert attachment.get_content_disposition() == 'attachment'
    assert attachment.get_filename() == 'anxin-board.html'
    embedded = 'data:image/png;base64,' + base64.b64encode(get_calligraphy_png()).decode('ascii')
    assert attachment.get_content().rstrip('\n') == html.replace('cid:' + CALLIGRAPHY_CID, embedded)
    assert 'cid:' not in attachment.get_content()
