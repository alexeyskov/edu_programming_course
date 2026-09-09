from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.core.security import hash_admin_token
from app.db.base import utcnow
from app.db.session import create_engine, create_session_factory
from app.integrations.errors import IntegrationError
from app.integrations.moodle_standard import MoodleStandardClient
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
    CourseSection,
)
from app.models.enums import (
    AssessmentStatus,
    AssessmentType,
    CourseRole,
    LMSProvider,
    TaskScope,
    TaskVersionStatus,
)
from app.models.identity import ExternalPrincipal, LMSConnection
from app.models.tasks import Assessment, AssessmentItem, TaskBankItem, TaskVersion
from app.services.common import canonical_hash


def _read_token(stdin: bool) -> str:
    if stdin:
        return sys.stdin.readline().rstrip("\r\n")
    token = getpass.getpass("Administrator token: ")
    confirmation = getpass.getpass("Repeat administrator token: ")
    if token != confirmation:
        raise ValueError("Tokens do not match")
    return token


def _hash_admin_token(args: argparse.Namespace) -> int:
    try:
        token = _read_token(args.stdin)
        print(hash_admin_token(token))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _normalize_base_url(value: str, *, debug: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not debug)
    ):
        raise ValueError("base URL must be an HTTPS origin without credentials/query/fragment")
    hostname = parsed.hostname.lower()
    default_port = (parsed.scheme == "https" and port in {None, 443}) or (
        parsed.scheme == "http" and port in {None, 80}
    )
    authority = hostname if default_port else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{authority}{path}"


async def _bootstrap_connection_async(args: argparse.Namespace) -> str:
    settings = Settings()
    base_url = _normalize_base_url(args.base_url, debug=settings.debug)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            row = await db.scalar(select(LMSConnection).where(LMSConnection.base_url == base_url))
            capabilities = {name: True for name in args.capability}
            config = {"auth_mode": args.auth_mode}
            if args.provider == LMSProvider.MOODLE.value and args.auth_mode == "PLUGINLESS":
                config["pluginless_transport"] = args.pluginless_transport
            if row is None:
                row = LMSConnection(
                    name=args.name,
                    provider=args.provider,
                    base_url=base_url,
                    enabled=not args.disabled,
                    config=config,
                    capabilities=capabilities,
                )
                db.add(row)
                await db.flush()
            else:
                row.name = args.name
                row.provider = args.provider
                row.enabled = not args.disabled
                row.config = {**(row.config or {}), **config}
                row.capabilities = capabilities
            await db.commit()
            return str(row.id)
    finally:
        await engine.dispose()


def _bootstrap_connection(args: argparse.Namespace) -> int:
    try:
        identifier = asyncio.run(_bootstrap_connection_async(args))
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(identifier)
    return 0


async def _diagnose_moodle_login_async(args: argparse.Namespace) -> int:
    settings = Settings()
    base_url = _normalize_base_url(args.base_url, debug=settings.debug)
    username = input("Moodle login: ").strip()
    password = getpass.getpass("Moodle password: ")
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": f"SFEDU-MMCS/{settings.app_build}"},
        ) as client:
            identity = await MoodleStandardClient(
                settings,
                client,
                base_url=base_url,
            ).authenticate(username, password)
    except IntegrationError as exc:
        print("status=error")
        print(f"code={exc.code}")
        print(f"type={type(exc).__name__}")
        print(f"detail={exc}")
        return 1
    finally:
        password = ""

    print("status=ok")
    print(f"moodle_user_id={identity.external_subject}")
    print(f"courses={len(identity.courses)}")
    print(f"functions={len(identity.functions)}")
    print("token=validated-but-not-printed")
    return 0


def _diagnose_moodle_login(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_diagnose_moodle_login_async(args))
    except (EOFError, KeyboardInterrupt):
        print("error: diagnostic cancelled", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


async def _seed_principal(
    db,
    *,
    connection: LMSConnection,
    course: Course,
    role: CourseRole,
) -> tuple[ExternalPrincipal, CourseMembership]:
    subject = f"dev-{role.value.lower()}"
    principal = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == connection.id,
            ExternalPrincipal.external_subject == subject,
        )
    )
    if principal is None:
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject=subject,
            display_name="Dev преподаватель" if role == CourseRole.TEACHER else "Dev студент",
            email=f"{subject}@example.invalid",
            active=True,
        )
        db.add(principal)
        await db.flush()
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == principal.id,
            CourseMembership.role == role.value,
        )
    )
    if membership is None:
        membership = CourseMembership(
            course_id=course.id,
            principal_id=principal.id,
            role=role.value,
            active=True,
            external_revision="demo-v1",
        )
        db.add(membership)
        await db.flush()
    else:
        membership.active = True
        membership.valid_until = None
        membership.synced_at = utcnow()
    return principal, membership


async def _seed_demo_async() -> dict[str, str]:
    settings = Settings()
    if not settings.debug:
        raise ValueError("seed-demo is refused unless APP_DEBUG=true")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            connection = await db.scalar(
                select(LMSConnection).where(LMSConnection.base_url == "https://mock-lms.local")
            )
            if connection is None:
                connection = LMSConnection(
                    name="Demo LMS",
                    provider=LMSProvider.MOCK.value,
                    base_url="https://mock-lms.local",
                    enabled=True,
                    config={"development_only": True},
                    capabilities={"courses": True, "memberships": True},
                )
                db.add(connection)
                await db.flush()
            course = await db.scalar(
                select(Course).where(
                    Course.connection_id == connection.id,
                    Course.external_id == "dev-cpp",
                )
            )
            if course is None:
                course = Course(
                    connection_id=connection.id,
                    external_id="dev-cpp",
                    title="Демонстрационный курс C/C++",
                    short_name="C/C++ Demo",
                    sync_status="CURRENT",
                    catalog_enabled=True,
                    catalog_added_at=utcnow(),
                )
                db.add(course)
                await db.flush()
            elif not course.catalog_enabled:
                course.catalog_enabled = True
                course.catalog_added_at = utcnow()
            section = await db.scalar(
                select(CourseSection).where(
                    CourseSection.course_id == course.id,
                    CourseSection.external_id == "dev-section-1",
                )
            )
            if section is None:
                section = CourseSection(
                    course_id=course.id,
                    external_id="dev-section-1",
                    title="Введение",
                    position=0,
                    visible=True,
                )
                db.add(section)
                await db.flush()
            group = await db.scalar(
                select(CourseGroup).where(
                    CourseGroup.course_id == course.id,
                    CourseGroup.external_id == "demo-group",
                )
            )
            if group is None:
                group = CourseGroup(
                    course_id=course.id,
                    external_id="demo-group",
                    name="1.1",
                    active=True,
                )
                db.add(group)
                await db.flush()
            teacher, _ = await _seed_principal(
                db,
                connection=connection,
                course=course,
                role=CourseRole.TEACHER,
            )
            student, student_membership = await _seed_principal(
                db,
                connection=connection,
                course=course,
                role=CourseRole.STUDENT,
            )
            link = await db.scalar(
                select(CourseMembershipGroup).where(
                    CourseMembershipGroup.coursemembership_id == student_membership.id,
                    CourseMembershipGroup.coursegroup_id == group.id,
                )
            )
            if link is None:
                db.add(
                    CourseMembershipGroup(
                        coursemembership_id=student_membership.id,
                        coursegroup_id=group.id,
                    )
                )
            task = await db.scalar(
                select(TaskBankItem).where(
                    TaskBankItem.course_id == course.id,
                    TaskBankItem.slug == "hello-cpp",
                )
            )
            if task is None:
                task = TaskBankItem(
                    scope=TaskScope.COURSE.value,
                    course_id=course.id,
                    slug="hello-cpp",
                    category="Основы",
                    tags=["iostream"],
                    created_by_id=teacher.id,
                )
                db.add(task)
                await db.flush()
            version = await db.scalar(
                select(TaskVersion).where(
                    TaskVersion.item_id == task.id,
                    TaskVersion.number == 1,
                )
            )
            if version is None:
                starter_files = [
                    {
                        "path": "main.cpp",
                        "content": "",
                        "language": None,
                        "read_only": False,
                    }
                ]
                content = {
                    "title": "Первая программа",
                    "statement": "Выведите приветствие в стандартный поток вывода.",
                    "language": "CPP",
                    "language_standard": "C++20",
                    "multi_file": False,
                    "starter_files": starter_files,
                    "build_profile": "cpp-gcc-c++20-single",
                    "public_examples": [],
                    "hidden_test_manifest": {},
                    "max_score": "10.00",
                    "difficulty": "1",
                    "ai_policy": {},
                }
                version = TaskVersion(
                    item_id=task.id,
                    number=1,
                    title=content["title"],
                    statement=content["statement"],
                    language="CPP",
                    language_standard="C++20",
                    multi_file=False,
                    starter_files=starter_files,
                    build_profile="cpp-gcc-c++20-single",
                    public_examples=[],
                    hidden_test_manifest={},
                    max_score=Decimal("10.00"),
                    difficulty="1",
                    ai_policy={},
                    content_hash=canonical_hash(content),
                    status=TaskVersionStatus.PUBLISHED.value,
                    authored_by_id=teacher.id,
                    published_at=utcnow(),
                )
                db.add(version)
                await db.flush()
            assessment = await db.scalar(
                select(Assessment).where(
                    Assessment.course_id == course.id,
                    Assessment.title == "Демонстрационная лабораторная",
                )
            )
            if assessment is None:
                assessment = Assessment(
                    course_id=course.id,
                    section_id=section.id,
                    type=AssessmentType.LAB.value,
                    title="Демонстрационная лабораторная",
                    instructions="Решите задачу в редакторе.",
                    max_score=Decimal("10.00"),
                    multi_file=False,
                    status=AssessmentStatus.PUBLISHED.value,
                    published_at=utcnow(),
                    created_by_id=teacher.id,
                )
                db.add(assessment)
                await db.flush()
            attached = await db.scalar(
                select(AssessmentItem).where(
                    AssessmentItem.assessment_id == assessment.id,
                    AssessmentItem.task_version_id == version.id,
                )
            )
            if attached is None:
                db.add(
                    AssessmentItem(
                        assessment_id=assessment.id,
                        task_version_id=version.id,
                        position=0,
                        points=Decimal("10.00"),
                        assignment_rule={},
                    )
                )
            await db.commit()
            return {
                "connection_id": str(connection.id),
                "course_id": str(course.id),
                "teacher_id": str(teacher.id),
                "student_id": str(student.id),
                "assessment_id": str(assessment.id),
            }
    finally:
        await engine.dispose()


def _seed_demo(_: argparse.Namespace) -> int:
    try:
        result = asyncio.run(_seed_demo_async())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for name, value in result.items():
        print(f"{name}={value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "hash-admin-token",
        help="Generate an Argon2id ADMIN_TOKEN_HASH without exposing the token in argv",
    )
    command.add_argument(
        "--stdin",
        action="store_true",
        help="read one token line from stdin (for a protected deployment secret pipe)",
    )
    command.set_defaults(handler=_hash_admin_token)

    connection = commands.add_parser(
        "bootstrap-connection",
        help="Create or update a non-secret LMS connection projection",
    )
    connection.add_argument("--name", required=True, help="display name")
    connection.add_argument(
        "--provider",
        choices=[item.value for item in LMSProvider],
        default=LMSProvider.MOODLE.value,
    )
    connection.add_argument("--base-url", required=True, help="canonical LMS base URL")
    connection.add_argument(
        "--auth-mode",
        choices=["PLUGINLESS", "BRIDGE"],
        default="PLUGINLESS",
        help="PLUGINLESS uses password login; BRIDGE uses the optional Moodle plugin",
    )
    connection.add_argument(
        "--pluginless-transport",
        choices=["PLAYWRIGHT", "MOBILE_TOKEN"],
        default="PLAYWRIGHT",
        help=(
            "password-login transport for Moodle PLUGINLESS connections; "
            "new connections default to PLAYWRIGHT"
        ),
    )
    connection.add_argument(
        "--capability",
        action="append",
        default=[],
        help="safe advertised capability name; repeat the option as needed",
    )
    connection.add_argument("--disabled", action="store_true")
    connection.set_defaults(handler=_bootstrap_connection)

    diagnostic = commands.add_parser(
        "diagnose-moodle-login",
        help="Interactively diagnose Moodle mobile-service login without printing secrets",
    )
    diagnostic.add_argument(
        "--base-url",
        required=True,
        help="canonical HTTPS Moodle base URL",
    )
    diagnostic.set_defaults(handler=_diagnose_moodle_login)

    demo = commands.add_parser(
        "seed-demo",
        help="Idempotently seed a local demo course (APP_DEBUG=true only)",
    )
    demo.set_defaults(handler=_seed_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
