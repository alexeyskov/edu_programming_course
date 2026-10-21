from __future__ import annotations

import base64
import struct
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import statement_images as image_api
from app.auth.context import require_auth
from app.core.credential_crypto import BROWSER_STATE_CREDENTIAL_KIND, encrypt_moodle_browser_state
from app.models.attempts import Attempt, MoodleQuizQuestion, Workspace
from app.models.identity import MoodleCredential
from app.models.tasks import Assessment, TaskVersion
from app.services import statement_images as images
from app.services.common import DomainError
from tests.test_api_integrity_ai_system import _context, api_bundle  # noqa: F401

BASE = "https://lms.test"
URL = BASE + "/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDuUAAAAASUVORK5CYII="
)
GIF = base64.b64decode("R0lGODdhAQABAIAAAP///wAAACwAAAAAAQABAAACAkQBADs=")
STATEMENT = "Вычислить сумму\n\n![\\\\sum_{k=1}^{N}](<" + URL + ">)\n\nВызвать функцию."


@pytest_asyncio.fixture
async def image_bundle(api_bundle):  # noqa: F811
    yield api_bundle


def _install_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(images.httpx, "AsyncClient", client)


def _response(content=PNG, **headers):
    return httpx.Response(
        200,
        headers={"content-type": "image/png", **headers},
        stream=httpx.ByteStream(content),
    )


@pytest.mark.parametrize(
    "path",
    [
        "/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png",
        "/pluginfile.php/123/question/questiontext/2/1/formula.png",
        "/pluginfile.php/123/question/questiontext/2/1/formula.png?forcedownload=0",
        "/file.php/12/image.jpg",
    ],
)
def test_accepts_only_moodle_image_routes(path):
    assert images.safe_statement_image_url(BASE + path, BASE)


@pytest.mark.parametrize(
    "url",
    [
        "http://lms.test/filter/tex/pix.php/a.png",
        "https://evil.test/pluginfile.php/a.png",
        "https://lms.test.evil.test/pluginfile.php/a.png",
        "https://lms.test:8443/pluginfile.php/a.png",
        "https://lms.test:0/pluginfile.php/a.png",
        "https://secret@lms.test/pluginfile.php/a.png",
        "https://lms.test/pluginfile.php/a.png#secret",
        "https://lms.test/login/index.php",
        "https://lms.test/admin/purgecaches.php",
        "https://lms.test/webservice/pluginfile.php/a.png?token=secret",
        "https://lms.test/pluginfile.php/a.png?token=secret",
        "https://lms.test/pluginfile.php/a.png?sesskey=secret",
        "https://lms.test/pluginfile.php/a.png?forcedownload=1&forcedownload=0",
        "https://lms.test/pluginfile.php/a.png?rev=not-a-revision",
        "https://lms.test/pluginfile.php/a.png?cache=",
        "https://lms.test/theme/image.php/theme/core/1/some-icon",
        "https://lms.test/pluginfile.php/1/solution.cpp",
        "https://lms.test/pluginfile.php/1/image.svg",
        "https://lms.test/pluginfile.php/../admin/purgecaches.php",
        "https://lms.test/pluginfile.php/%2e%2e/admin/purgecaches.php",
        "https://lms.test/pluginfile.php/%252e%252e/admin/purgecaches.php",
        "https://lms.test/pluginfile.php/%25252e%25252e/admin/purgecaches.php",
        "https://lms.test/pluginfile.php/%00/a.png",
        "https://lms.test/pluginfile.php/\\admin/a.png",
        "https://lms.test/pluginfile.php/a.png\r\nSecret: secret",
        "data:image/png;base64,aGVsbG8=",
        "//lms.test/pluginfile.php/a.png",
        "https://lms.test/pluginfile.php/a.png?" + "&".join("rev=1" for _ in range(9)),
    ],
)
def test_rejects_untrusted_or_state_changing_urls(url):
    assert not images.safe_statement_image_url(url, BASE)


def test_handles_moodle_installed_under_path_prefix():
    assert images.safe_statement_image_url(
        BASE + "/moodle/pluginfile.php/1/image.png",
        BASE + "/moodle/",
    )
    assert not images.safe_statement_image_url(
        BASE + "/pluginfile.php/1/image.png", BASE + "/moodle"
    )


@pytest.mark.parametrize(
    ("content", "media_type"),
    [
        (PNG, "image/png"),
        (b"GIF89a" + struct.pack("<HH", 1, 1), "image/gif"),
        (b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 14, "image/webp"),
        (
            b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x01\x00\x01\x01",
            "image/jpeg",
        ),
    ],
)
def test_recognizes_only_raster_headers_with_bounded_dimensions(content, media_type):
    assert images._image_valid(content, media_type)
    assert not images._image_valid(content, "image/svg+xml")
    assert not images._image_valid(content[:4], media_type)


async def test_fetches_raster_with_only_matching_unexpired_cookies(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["cookie"] == "MoodleSession=own-session"
        assert request.headers["accept-encoding"] == "identity"
        return _response()

    _install_transport(monkeypatch, handle)
    cookies = [
        {"name": "MoodleSession", "value": "own-session", "domain": "lms.test", "path": "/"},
        {"name": "Foreign", "value": "other", "domain": "evil.test", "path": "/"},
        {"name": "Expired", "value": "old", "domain": "lms.test", "expires": 1},
        {"name": "Unsafe", "value": "cookie\r\nInjected: a", "domain": "lms.test"},
    ]
    result = await images.fetch_statement_image(
        url=URL, base_url=BASE, storage_state={"cookies": cookies}
    )
    assert result == images.StatementImage(PNG, "image/png")
    assert len(calls) == 1


async def test_allows_public_tex_without_moodle_cookie(monkeypatch):
    def handle(request):
        assert "cookie" not in request.headers
        return _response()

    _install_transport(monkeypatch, handle)
    assert (await images.fetch_statement_image(url=URL, base_url=BASE)).content == PNG


async def test_normalizes_legacy_moodle_tex_gif_mislabeled_as_png(monkeypatch):
    _install_transport(monkeypatch, lambda _request: _response(GIF))
    result = await images.fetch_statement_image(url=URL, base_url=BASE)
    assert result == images.StatementImage(GIF, "image/gif")


async def test_does_not_relax_mime_checks_for_ordinary_picture_routes(monkeypatch):
    _install_transport(monkeypatch, lambda _request: _response(GIF))
    with pytest.raises(DomainError) as exc:
        await images.fetch_statement_image(
            url=BASE + "/pluginfile.php/1/diagram.png", base_url=BASE
        )
    assert exc.value.status_code == 502


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"login")
        ),
        _response(b"<svg onload='steal()'/>", **{"content-type": "image/svg+xml"}),
        _response(b"<html>login</html>"),
        _response(PNG, **{"content-length": str(images.MAX_IMAGE_BYTES + 1)}),
        _response(PNG + b"x" * images.MAX_IMAGE_BYTES),
        _response(PNG, **{"content-length": str(len(PNG) + 1)}),
        _response(PNG, **{"content-encoding": "gzip"}),
        _response(PNG[:16] + struct.pack(">II", 65535, 65535) + PNG[24:]),
        httpx.Response(403, stream=httpx.ByteStream(b"forbidden")),
    ],
)
async def test_rejects_login_svg_invalid_oversized_and_compressed_responses(monkeypatch, response):
    _install_transport(monkeypatch, lambda _request: response)
    with pytest.raises(DomainError) as exc:
        await images.fetch_statement_image(url=URL, base_url=BASE)
    assert exc.value.status_code == 502
    assert "login" not in exc.value.message


@pytest.mark.parametrize(
    "target", ["https://evil.test/image.png", "/login/index.php", "/admin/purgecaches.php"]
)
async def test_rejects_redirect_before_sending_cookies_to_other_url(monkeypatch, target):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": target})

    _install_transport(monkeypatch, handle)
    with pytest.raises(DomainError):
        await images.fetch_statement_image(url=URL, base_url=BASE)
    assert len(calls) == 1


async def test_accepts_bounded_same_origin_image_redirect(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "/pluginfile.php/1/formula.png"})
        return _response()

    _install_transport(monkeypatch, handle)
    assert (await images.fetch_statement_image(url=URL, base_url=BASE)).content == PNG
    assert len(calls) == 2


async def _install_statement(sessions, seed):
    async with sessions() as db, db.begin():
        attempt = await db.get(Attempt, seed.attempt_id)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        version.statement = STATEMENT
        assessment = await db.get(Assessment, seed.assessment_id)
        assessment.instructions = STATEMENT


@pytest.mark.parametrize("kind", ["attempt", "submission", "assessment"])
async def test_authorized_image_is_private_and_does_not_forward_upstream_headers(
    image_bundle,
    monkeypatch,
    kind,
):
    app, sessions, _, seed, holder = image_bundle
    await _install_statement(sessions, seed)
    scope_id = getattr(seed, f"{kind}_id")
    if kind != "submission":
        holder["auth"] = _context(seed.student_id, roles=("STUDENT",))

    _install_transport(
        monkeypatch,
        lambda _request: _response(
            **{
                "set-cookie": "secret=never-forward",
                "x-secret": "private",
                "content-location": "secret",
            },
        ),
    )
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/statement-images/{kind}/{scope_id}",
            params={"url": URL},
        )
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "secret" not in str(dict(response.headers))


@pytest.mark.parametrize("kind", ["attempt", "submission", "assessment"])
async def test_tex_image_response_uses_verified_gif_type_and_nosniff(
    image_bundle, monkeypatch, kind,
):
    app, sessions, _, seed, holder = image_bundle
    await _install_statement(sessions, seed)
    if kind != "submission":
        holder["auth"] = _context(seed.student_id, roles=("STUDENT",))
    _install_transport(monkeypatch, lambda _request: _response(GIF))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/statement-images/{kind}/{getattr(seed, f'{kind}_id')}", params={"url": URL},
        )
    assert response.status_code == 200
    assert response.content == GIF
    assert response.headers["content-type"] == "image/gif"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("kind", ["attempt", "submission", "assessment"])
async def test_does_not_download_undeclared_image(image_bundle, monkeypatch, kind):
    app, sessions, _, seed, holder = image_bundle
    await _install_statement(sessions, seed)
    if kind != "submission":
        holder["auth"] = _context(seed.student_id, roles=("STUDENT",))

    async def forbidden(**_kwargs):
        raise AssertionError("No network request may happen for an undeclared image")

    monkeypatch.setattr(image_api, "fetch_statement_image", forbidden)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/statement-images/{kind}/{getattr(seed, f'{kind}_id')}",
            params={"url": BASE + "/pluginfile.php/secret/solution.cpp"},
        )
    assert response.status_code == 404


@pytest.mark.parametrize("kind", ["attempt", "submission", "assessment"])
async def test_unauthorized_scope_never_loads_image(image_bundle, monkeypatch, kind):
    app, sessions, _, seed, holder = image_bundle
    await _install_statement(sessions, seed)
    holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))

    async def forbidden(**_kwargs):
        raise AssertionError("No network request may happen across authorization scopes")

    monkeypatch.setattr(image_api, "fetch_statement_image", forbidden)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/statement-images/{kind}/{getattr(seed, f'{kind}_id')}",
            params={"url": URL},
        )
    assert response.status_code in {403, 404}


async def test_unauthenticated_image_request_is_rejected(image_bundle, monkeypatch):
    app, sessions, _, seed, _ = image_bundle
    await _install_statement(sessions, seed)
    app.dependency_overrides.pop(require_auth)

    async def forbidden(**_kwargs):
        raise AssertionError("No network request may happen without an authenticated viewer")

    monkeypatch.setattr(image_api, "fetch_statement_image", forbidden)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/statement-images/attempt/{seed.attempt_id}",
            params={"url": URL},
        )
    assert response.status_code == 401


@pytest.mark.parametrize("expired", [False, True])
async def test_uses_only_viewers_own_current_browser_credential(image_bundle, monkeypatch, expired):
    app, sessions, settings, seed, holder = image_bundle
    await _install_statement(sessions, seed)
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
    async with sessions() as db, db.begin():
        for principal_id, value in [
            (seed.student_id, "student-secret"),
            (seed.teacher_id, "teacher-secret"),
        ]:
            state = {
                "cookies": [
                    {"name": "MoodleSession", "value": value, "domain": "lms.test", "path": "/"}
                ]
            }
            db.add(
                MoodleCredential(
                    connection_id=seed.connection_id,
                    principal_id=principal_id,
                    kind=BROWSER_STATE_CREDENTIAL_KIND,
                    encrypted_secret=encrypt_moodle_browser_state(
                        state,
                        settings,
                        connection_id=seed.connection_id,
                        principal_id=principal_id,
                    ),
                    expires_at=datetime.now(UTC) - timedelta(seconds=1) if expired else None,
                )
            )

    def handle(request):
        assert request.headers.get("cookie", "") == (
            "" if expired else "MoodleSession=teacher-secret"
        )
        return _response()

    _install_transport(monkeypatch, handle)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/statement-images/submission/{seed.submission_id}",
            params={"url": URL},
        )
    assert response.status_code == 200
    assert "teacher-secret" not in str(response.headers)
    assert "student-secret" not in str(response.headers)


async def test_child_attempt_images_use_own_question_and_parent_intro_not_sibling(
    image_bundle,
    monkeypatch,
):
    app, sessions, _, seed, holder = image_bundle
    holder["auth"] = _context(seed.student_id, roles=("STUDENT",))
    parent_url = BASE + "/pluginfile.php/10/intro/diagram.png"
    sibling_url = BASE + "/pluginfile.php/20/question/sibling.png"
    child_url = BASE + "/pluginfile.php/30/question/child.png"
    async with sessions() as db, db.begin():
        root = await db.get(Attempt, seed.attempt_id)
        parent = await db.get(Assessment, seed.assessment_id)
        parent.instructions = f"![Введение](<{parent_url}>)"
        sibling_version = await db.get(TaskVersion, root.assigned_task_version_id)
        sibling_version.statement = f"![Другой вопрос](<{sibling_url}>)"
        child_version = TaskVersion(
            item_id=sibling_version.item_id,
            number=2,
            title="Question 2",
            statement=f"![Этот вопрос](<{child_url}>)",
            content_hash="b" * 64,
            authored_by_id=seed.teacher_id,
        )
        child_assessment = Assessment(
            course_id=seed.course_id,
            title="Question 2",
            status="PUBLISHED",
            instructions=f"![Not the parent introduction](<{sibling_url}>)",
            created_by_id=seed.teacher_id,
        )
        db.add_all([child_version, child_assessment])
        await db.flush()
        child = Attempt(
            assessment_id=child_assessment.id,
            assigned_task_version_id=child_version.id,
            principal_id=seed.student_id,
        )
        db.add(child)
        await db.flush()
        db.add(Workspace(attempt_id=child.id))
        db.add_all(
            [
                MoodleQuizQuestion(
                    root_attempt_id=root.id,
                    attempt_id=root.id,
                    question_slot="1",
                    position=0,
                    title="Question 1",
                    question_max_mark=10,
                ),
                MoodleQuizQuestion(
                    root_attempt_id=root.id,
                    attempt_id=child.id,
                    question_slot="2",
                    position=1,
                    title="Question 2",
                    question_max_mark=10,
                ),
            ]
        )
        child_id = child.id
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return _response()

    _install_transport(monkeypatch, handle)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        for url in (parent_url, child_url, sibling_url):
            response = await client.get(
                f"/api/v1/statement-images/attempt/{child_id}",
                params={"url": url},
            )
            assert response.status_code == (404 if url == sibling_url else 200)
    assert calls == [parent_url, child_url]
