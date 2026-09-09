from __future__ import annotations

from datetime import timedelta

from app.core.credential_crypto import BROWSER_STATE_CREDENTIAL_KIND
from app.db.base import utcnow
from app.models.identity import ExternalPrincipal, LMSConnection, MoodleCredential


async def test_moodle_credentials_support_browser_session_leases_without_changing_mobile_kind(
    db,
) -> None:
    connection = LMSConnection(
        name="MMCS Moodle",
        provider="MOODLE",
        base_url="https://edu.mmcs.sfedu.ru",
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="42",
        display_name="Student",
    )
    db.add(principal)
    await db.flush()

    mobile = MoodleCredential(
        connection_id=connection.id,
        principal_id=principal.id,
        encrypted_secret="encrypted-mobile-token",
    )
    lease_expires_at = utcnow() + timedelta(seconds=30)
    browser = MoodleCredential(
        connection_id=connection.id,
        principal_id=principal.id,
        kind=BROWSER_STATE_CREDENTIAL_KIND,
        encrypted_secret="encrypted-browser-state",
        lease_owner="worker-7",
        lease_expires_at=lease_expires_at,
        last_used_at=utcnow(),
    )
    db.add_all([mobile, browser])
    await db.flush()

    assert mobile.kind == "MOBILE_TOKEN"
    assert mobile.revision == 1
    assert browser.kind == BROWSER_STATE_CREDENTIAL_KIND
    assert browser.revision == 1
    assert browser.status == "ACTIVE"
    assert browser.lease_owner == "worker-7"
    assert browser.lease_expires_at == lease_expires_at
