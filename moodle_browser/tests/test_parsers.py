from __future__ import annotations

from pathlib import Path

import pytest

from moodle_browser.parsers import (
    MoodleMarkupError,
    has_authenticated_markup,
    merge_course_sections,
    parse_activity_settings,
    parse_activity_user_override_edit,
    parse_activity_user_override_index,
    parse_course_links,
    parse_course_page,
    parse_course_section_links,
    parse_identity,
    parse_participants_page,
    parse_quiz_essay_question_edit,
    parse_quiz_question_summary,
    parse_quiz_random_question_bank,
    teacher_controls_present,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://edu.mmcs.sfedu.ru"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_dashboard_parses_stable_identity_and_only_same_origin_courses() -> None:
    html = fixture("dashboard.html")
    assert has_authenticated_markup(html, BASE_URL)
    assert parse_identity(html, BASE_URL) == {
        "external_subject": "42",
        "display_name": "Алексей Преподаватель",
        "email": "",
        "locale": "ru",
    }
    assert parse_course_links(html, BASE_URL) == [
        {
            "external_id": "549",
            "title": "Программирование C/C++",
            "short_name": "CPP",
            "role": "UNKNOWN",
        }
    ]


def test_profile_hidden_control_supplies_current_user_id_when_menu_omits_it() -> None:
    html = """
    <html lang="ru"><body>
      <div class="usermenu">
        <span class="usertext">Алексей Преподаватель</span>
        <a href="/user/profile.php">О пользователе</a>
        <a href="/login/logout.php?sesskey=opaque">Выход</a>
      </div>
      <header class="page-header-headings"><h1>Алексей Преподаватель</h1></header>
      <form action="/user/profile.php" method="get">
        <input type="hidden" name="id" value="4376">
        <input type="hidden" name="edit" value="1">
      </form>
    </body></html>
    """

    assert parse_identity(html, BASE_URL) == {
        "external_subject": "4376",
        "display_name": "Алексей Преподаватель",
        "email": "",
        "locale": "ru",
    }


def test_identity_drops_theme_avatar_initials_from_visible_name() -> None:
    html = """
    <html lang="ru"><body>
      <div class="usermenu">
        <span class="usertext">АК Алексей Коваленко</span>
        <a href="/user/profile.php?id=4376">Профиль</a>
        <a href="/login/logout.php?sesskey=opaque">Выход</a>
      </div>
    </body></html>
    """

    assert parse_identity(html, BASE_URL)["display_name"] == "Коваленко Алексей"


def test_identity_does_not_accept_hidden_id_from_an_unrelated_form() -> None:
    html = """
    <div class="usermenu"><span class="usertext">Пользователь</span></div>
    <form action="/course/view.php"><input name="id" value="549"></form>
    """
    with pytest.raises(MoodleMarkupError, match="stable numeric user id"):
        parse_identity(html, BASE_URL)


def test_course_uses_activity_wrapper_data_id_and_modtype_contract() -> None:
    course = parse_course_page(fixture("course.html"), BASE_URL, "549")
    assert course["teacher_controls"] is True
    assert course["grade_controls"] is True
    assert course["title"] == "Практикум по C/C++"
    assert course["short_name"] == "CPP-2026"
    assert len(course["sections"]) == 1
    section = course["sections"][0]
    assert section["external_id"] == "10"
    assert [activity["cmid"] for activity in section["activities"]] == [777, 778]
    assert section["activities"][0]["module"] == "assign"
    assert section["activities"][0]["name"] == "Лабораторная 1"
    assert section["activities"][1]["visible"] is False


def test_course_accepts_legacy_module_id_only_when_canonical_link_agrees() -> None:
    html = """
    <div class='page-header-headings'><h1>C++</h1></div>
    <li id='section-1' class='section main' data-sectionid='10'>
      <h3 class='sectionname'>Самостоятельные</h3>
      <li id='module-30354' class='activity quiz modtype_quiz'>
        <div class='activityname'><a href='/mod/quiz/view.php?id=30354'>Самостоятельная 1</a></div>
      </li>
      <li id='module-30355' class='activity assign modtype_assign'>
        <a href='/mod/assign/view.php?id=99999'>Несогласованная ссылка</a>
      </li>
    </li>
    """
    course = parse_course_page(html, BASE_URL, "549")
    assert course["sections"][0]["activities"] == [
        {
            "cmid": 30354,
            "instance_id": 0,
            "module": "quiz",
            "name": "Самостоятельная 1",
            "visible": True,
            "uservisible": True,
            "url": f"{BASE_URL}/mod/quiz/view.php?id=30354",
            "opens_at": 0,
            "due_at": 0,
            "cutoff_at": 0,
        }
    ]


def test_activity_settings_and_single_essay_summary_are_bounded() -> None:
    settings_html = """
    <html><body class='path-mod-quiz' data-courseid='549'>
      <form class='mform'>
        <input name='coursemodule' value='30354'>
        <input name='course' value='549'>
        <input name='modulename' value='quiz'>
        <input name='instance' value='901'>
        <textarea name='intro[text]'>&lt;p&gt;Решите задачу&lt;/p&gt;</textarea>
        <input name='timeopen' value='1787600000'>
        <input name='timeclose' value='1787607200'>
        <select name='grade'><option selected value='10'>10</option></select>
        <select name='attempts'><option selected value='1'>1</option></select>
        <select name='grademethod'><option selected value='4'>Last attempt</option></select>
        <input name='timelimit[number]' value='90'>
        <select name='timelimit[timeunit]'><option selected value='60'>minutes</option></select>
      </form>
    </body></html>
    """
    assert parse_activity_settings(
        settings_html,
        course_id="549",
        cmid=30354,
        module="quiz",
    ) == {
        "description": "Решите задачу",
        "instance_id": 901,
        "opens_at": 1787600000,
        "due_at": 1787607200,
        "cutoff_at": 0,
        "grade_max": 10.0,
        "attempt_limit": 1,
        "quiz_grading_method": "LAST",
        "quiz_grading_method_confirmed": True,
        "duration_seconds": 5400,
        "settings_confirmed": True,
        "statement_confirmed": False,
        "schedule_confirmed": True,
        "duration_confirmed": True,
        "grade_confirmed": True,
        "attempt_policy_confirmed": True,
    }

    quiz_html = """
    <html><body class='course-549'>
      <input name='cmid' value='30354'>
      <ul class='slots'><li class='slot qtype_essay' data-slot='1' data-questionid='77'>
        <img src='/question/type/essay/icon.svg' title='Эссе'>
      </li></ul>
    </body></html>
    """
    assert parse_quiz_question_summary(quiz_html, course_id="549", cmid=30354) == {
        "question_count": 1,
        "essay_question_count": 1,
        "random_question_count": 0,
        "random_essay_confirmed": False,
        "statement_deferred": False,
        "import_supported": True,
    }


def test_live_activity_settings_keep_moodle_schedule_grade_attempts_and_paragraphs() -> None:
    assign = parse_activity_settings(
        fixture("activity_settings_live_assign.html"),
        course_id="549",
        cmid=23457,
        module="assign",
    )
    assert assign == {
        "description": (
            "Реализуйте класс.\n\n"
            "Требования:\n"
            "Не менять интерфейс.\n\n"
            "Первый пункт\n\n"
            "Второй пункт"
        ),
        "instance_id": 7828,
        "opens_at": 1_707_342_840,
        "due_at": 0,
        "cutoff_at": 0,
        "grade_max": 10.0,
        "attempt_limit_unlimited": True,
        "settings_confirmed": True,
        "statement_confirmed": True,
        "schedule_confirmed": True,
        "duration_confirmed": True,
        "grade_confirmed": True,
        "attempt_policy_confirmed": True,
    }

    quiz = parse_activity_settings(
        fixture("activity_settings_live_quiz.html"),
        course_id="549",
        cmid=30354,
        module="quiz",
    )
    assert quiz == {
        "description": "Самостоятельная работа.",
        "instance_id": 901,
        "opens_at": 1_675_285_200,
        "due_at": 1_675_290_600,
        "cutoff_at": 0,
        "grade_max": 3.0,
        "attempt_limit_unlimited": True,
        "quiz_grading_method": "LAST",
        "quiz_grading_method_confirmed": True,
        "duration_seconds": 1_800,
        "settings_confirmed": True,
        "statement_confirmed": False,
        "schedule_confirmed": True,
        "duration_confirmed": True,
        "grade_confirmed": True,
        "attempt_policy_confirmed": True,
    }


def test_assignment_without_reopening_has_one_attempt_and_scale_is_not_a_score() -> None:
    html = (
        fixture("activity_settings_live_assign.html")
        .replace(
            '<option selected value="manual">Вручную</option>',
            '<option selected value="none">Никогда</option>',
        )
        .replace(
            '<option selected value="point">Баллы</option>',
            '<option selected value="scale">Шкала</option>',
        )
    )
    result = parse_activity_settings(html, course_id="549", cmid=23457, module="assign")
    assert result["attempt_limit"] == 1
    assert "attempt_limit_unlimited" not in result
    assert "grade_max" not in result
    assert result["grade_confirmed"] is False


def test_disabled_quiz_time_limit_and_direct_disabled_date_are_ignored() -> None:
    html = (
        fixture("activity_settings_live_quiz.html")
        .replace(
            'type="checkbox" checked name="timelimit[enabled]"',
            'type="checkbox" name="timelimit[enabled]"',
        )
        .replace(
            'type="checkbox" checked name="timeclose[enabled]"',
            'type="checkbox" name="timeclose[enabled]"',
        )
    )
    result = parse_activity_settings(html, course_id="549", cmid=30354, module="quiz")
    assert result["due_at"] == 0
    assert "duration_seconds" not in result
    assert result["schedule_confirmed"] is True
    assert result["duration_confirmed"] is True


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("1", "HIGHEST"), ("2", "AVERAGE"), ("3", "FIRST"), ("4", "LAST")],
)
def test_quiz_grading_method_is_captured_from_authoritative_settings(
    raw_value: str,
    expected: str,
) -> None:
    html = fixture("activity_settings_live_quiz.html").replace(
        '<option selected value="4">Последняя попытка</option>',
        f'<option selected value="{raw_value}">{expected}</option>',
    )
    result = parse_activity_settings(html, course_id="549", cmid=30354, module="quiz")
    assert result["quiz_grading_method"] == expected
    assert result["quiz_grading_method_confirmed"] is True
    assert result["attempt_policy_confirmed"] is True


def test_quiz_attempt_policy_is_unconfirmed_without_grading_method() -> None:
    html = fixture("activity_settings_live_quiz.html").replace(
        '<select name="grademethod"><option selected value="4">'
        "Последняя попытка</option></select>",
        "",
    )
    result = parse_activity_settings(html, course_id="549", cmid=30354, module="quiz")
    assert "quiz_grading_method" not in result
    assert result["quiz_grading_method_confirmed"] is False
    assert result["attempt_policy_confirmed"] is False


def test_quiz_user_override_index_and_edit_preserve_individual_limits() -> None:
    index_html = """
    <html><body id="page-mod-quiz-overrides"
      class="path-mod-quiz course-549 cmid-30354">
      <a href="/course/view.php?id=549">C++</a>
      <table><tbody><tr>
        <td><a href="/user/view.php?id=104684&amp;course=549">Test User</a></td>
        <td><a href="/mod/quiz/overrideedit.php?id=8123">Изменить</a></td>
      </tr></tbody></table>
    </body></html>
    """
    references = parse_activity_user_override_index(
        index_html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        module="quiz",
    )
    assert references == [
        {
            "override_id": 8123,
            "user_id": "104684",
            "display_name": "Test User",
            "edit_url": f"{BASE_URL}/mod/quiz/overrideedit.php?id=8123",
        }
    ]

    edit_html = """
    <html><body class="path-mod-quiz course-549 cmid-30354">
      <form class="mform">
        <input name="id" value="8123">
        <input name="userid" value="104684">
        <input type="checkbox" name="timeopen[enabled]" checked>
        <select name="timeopen[year]"><option selected value="2023">2023</option></select>
        <select name="timeopen[month]"><option selected value="2">2</option></select>
        <select name="timeopen[day]"><option selected value="2">2</option></select>
        <select name="timeopen[hour]"><option selected value="0">0</option></select>
        <select name="timeopen[minute]"><option selected value="0">0</option></select>
        <input type="checkbox" name="timeclose[enabled]" checked>
        <select name="timeclose[year]"><option selected value="2026">2026</option></select>
        <select name="timeclose[month]"><option selected value="10">10</option></select>
        <select name="timeclose[day]"><option selected value="2">2</option></select>
        <select name="timeclose[hour]"><option selected value="0">0</option></select>
        <select name="timeclose[minute]"><option selected value="0">0</option></select>
        <input type="checkbox" name="timelimit[enabled]" checked>
        <input name="timelimit[number]" value="30">
        <select name="timelimit[timeunit]"><option selected value="60">minutes</option></select>
        <select name="attempts"><option selected value="10">10</option></select>
      </form>
    </body></html>
    """
    assert parse_activity_user_override_edit(
        edit_html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        module="quiz",
        override_id=8123,
        user_id="104684",
        display_name="Test User",
    ) == {
        "override_id": 8123,
        "user_id": "104684",
        "display_name": "Test User",
        "opens_at": 1675285200,
        "due_at": 1790888400,
        "cutoff_at": 0,
        "opens_at_overridden": True,
        "due_at_overridden": True,
        "cutoff_at_overridden": False,
        "duration_overridden": True,
        "duration_seconds": 1800,
        "attempt_limit_overridden": True,
        "attempt_limit_unlimited": False,
        "attempt_limit": 10,
        "confirmed": True,
    }


def test_current_quiz_override_pages_use_body_and_form_action_as_identity() -> None:
    """Moodle 5.2 no longer needs to repeat the override id as a form input."""

    index_html = """
    <html><body id="page-mod-quiz-overrides"
      class="path-mod-quiz course-549 cmid-30354 limitedwidth">
      <table><tbody><tr>
        <td><a href="/user/view.php?id=104685&amp;course=549">Test2 User2</a></td>
        <td><a href="/mod/quiz/overrideedit.php?id=9124" title="Изменить">Edit</a></td>
      </tr></tbody></table>
    </body></html>
    """
    references = parse_activity_user_override_index(
        index_html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        module="quiz",
    )
    assert references == [
        {
            "override_id": 9124,
            "user_id": "104685",
            "display_name": "Test2 User2",
            "edit_url": f"{BASE_URL}/mod/quiz/overrideedit.php?id=9124",
        }
    ]

    edit_html = """
    <html><body id="page-mod-quiz-overrideedit"
      class="path-mod-quiz course-549 cmid-30354 limitedwidth">
      <form class="mform" id="mform1"
        action="/mod/quiz/overrideedit.php?id=9124" method="post">
        <input type="hidden" name="userid" value="104685">
        <input type="checkbox" name="timeopen[enabled]">
        <select name="timeopen[year]"><option selected value="2026">2026</option></select>
        <select name="timeopen[month]"><option selected value="9">9</option></select>
        <select name="timeopen[day]"><option selected value="1">1</option></select>
        <select name="timeopen[hour]"><option selected value="9">9</option></select>
        <select name="timeopen[minute]"><option selected value="0">0</option></select>
        <input type="checkbox" name="timeclose[enabled]" checked>
        <select name="timeclose[year]"><option selected value="2026">2026</option></select>
        <select name="timeclose[month]"><option selected value="10">10</option></select>
        <select name="timeclose[day]"><option selected value="2">2</option></select>
        <select name="timeclose[hour]"><option selected value="0">0</option></select>
        <select name="timeclose[minute]"><option selected value="0">0</option></select>
        <input type="checkbox" name="timelimit[enabled]" checked>
        <input name="timelimit[number]" value="30">
        <select name="timelimit[timeunit]"><option selected value="60">minutes</option></select>
        <select name="attempts"><option selected value="1">1</option></select>
      </form>
    </body></html>
    """
    parsed = parse_activity_user_override_edit(
        edit_html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        module="quiz",
        override_id=9124,
        user_id="104685",
        display_name="Test2 User2",
    )
    assert parsed["override_id"] == 9124
    assert parsed["user_id"] == "104685"
    assert parsed["opens_at_overridden"] is False
    assert parsed["due_at_overridden"] is True
    assert parsed["duration_seconds"] == 1800
    assert parsed["attempt_limit"] == 1
    assert parsed["confirmed"] is True


def test_quiz_override_edit_rejects_wrong_form_action_id() -> None:
    html = """
    <html><body class="path-mod-quiz course-549 cmid-30354">
      <form class="mform" action="/mod/quiz/overrideedit.php?id=9999">
        <input name="userid" value="104684">
      </form>
    </body></html>
    """
    with pytest.raises(MoodleMarkupError, match="another override"):
        parse_activity_user_override_edit(
            html,
            base_url=BASE_URL,
            course_id="549",
            cmid=30354,
            module="quiz",
            override_id=8123,
            user_id="104684",
            display_name="Test User",
        )


def test_quiz_user_override_rejects_cross_activity_edit_form() -> None:
    html = """
    <html><body class="path-mod-quiz course-549 cmid-30355">
      <form class="mform"><input name="cmid" value="30355"><input name="id" value="8123">
        <input name="timeopen" value="1700000000"><input name="timeclose" value="1790888400">
        <input name="attempts" value="10">
      </form>
    </body></html>
    """
    with pytest.raises(MoodleMarkupError, match="another activity"):
        parse_activity_user_override_edit(
            html,
            base_url=BASE_URL,
            course_id="549",
            cmid=30354,
            module="quiz",
            override_id=8123,
            user_id="104684",
            display_name="Test User",
        )


def test_single_essay_edit_link_and_read_only_question_form_supply_statement() -> None:
    summary = parse_quiz_question_summary(
        fixture("quiz_edit_single_essay.html"),
        course_id="549",
        cmid=30354,
        base_url=BASE_URL,
    )
    assert summary == {
        "question_count": 1,
        "essay_question_count": 1,
        "random_question_count": 0,
        "random_essay_confirmed": False,
        "statement_deferred": False,
        "import_supported": True,
        "_essay_edit_url": (
            f"{BASE_URL}/question/bank/editquestion/question.php?"
            "cmid=30354&id=7711&returnurl=%2Fmod%2Fquiz%2Fedit.php%3Fcmid%3D30354"
        ),
        "_essay_question_id": "7711",
        "_essay_question_query_name": "id",
    }
    assert parse_quiz_essay_question_edit(
        fixture("question_edit_essay.html"),
        course_id="549",
        cmid=30354,
        question_id="7711",
    ) == {
        "description": (
            "Реализовать класс «Комплексное число».\n\n"
            "Перегрузить операции:\n"
            "+ — сложение;\n"
            "<< — вывод в поток."
        ),
        "answer_transport": "ESSAY_ONLINE_TEXT",
        "statement_confirmed": True,
    }


def test_moodle_five_preview_link_supplies_read_only_question_reference() -> None:
    html = f"""
    <html><body class="course-549 path-mod-quiz">
      <input name="cmid" value="30354">
      <ul class="slots">
        <li id="slot-991" class="activity essay qtype_essay slot">
          <span class="instancename">Programming task</span>
          <a class="preview" title="Preview question"
             href="{BASE_URL}/question/bank/previewquestion/preview.php?id=7711&amp;cmid=30354&amp;behaviour=deferredfeedback">
            Preview
          </a>
        </li>
      </ul>
    </body></html>
    """

    assert parse_quiz_question_summary(
        html,
        course_id="549",
        cmid=30354,
        base_url=BASE_URL,
    ) == {
        "question_count": 1,
        "essay_question_count": 1,
        "random_question_count": 0,
        "random_essay_confirmed": False,
        "statement_deferred": False,
        "import_supported": True,
        "_essay_edit_url": (
            f"{BASE_URL}/question/bank/editquestion/question.php?cmid=30354&id=7711"
        ),
        "_essay_question_id": "7711",
        "_essay_question_query_name": "id",
    }


def test_random_quiz_slot_exposes_only_its_bounded_question_bank_reference() -> None:
    filter_value = "%7B%22category%22%3A%2210197%2C123%22%7D"
    html = f"""
    <html><body class="course-549 path-mod-quiz">
      <input name="cmid" value="30354">
      <ul class="slots"><li class="slot" data-slot="1">
        <a data-action="editrandomquestion">Настроить случайный вопрос</a>
        <a class="mod_quiz_random_qbank_link"
           href="/question/edit.php?cmid=30354&amp;filter={filter_value}">Банк вопросов</a>
      </li></ul>
    </body></html>
    """

    assert parse_quiz_question_summary(
        html,
        course_id="549",
        cmid=30354,
        base_url=BASE_URL,
    ) == {
        "question_count": 1,
        "essay_question_count": 0,
        "random_question_count": 1,
        "random_essay_confirmed": False,
        "statement_deferred": False,
        "import_supported": False,
        "_random_qbank_url": (f"{BASE_URL}/question/edit.php?cmid=30354&filter={filter_value}"),
    }


def test_random_question_bank_requires_complete_nonempty_all_essay_page() -> None:
    html = """
    <html><body class="course-549 path-question">
      <a href="/course/view.php?id=549">C++</a>
      <a href="/login/logout.php?sesskey=opaque">Выход</a>
      <table id="categoryquestions" class="question-bank-table"><tbody>
        <tr><td class="qtype"><img src="/question/type/essay/pix/icon.svg" alt="Эссе"></td></tr>
        <tr><td class="qtype"><img src="/question/type/essay/pix/icon.svg" title="Essay"></td></tr>
      </tbody></table>
    </body></html>
    """
    assert parse_quiz_random_question_bank(
        html,
        base_url=BASE_URL,
        course_id="549",
    ) == {
        "question_count": 2,
        "essay_question_count": 2,
        "all_essay": True,
        "complete": True,
    }

    mixed = html.replace(
        '/question/type/essay/pix/icon.svg" title="Essay"',
        '/question/type/multichoice/pix/icon.svg" title="Multiple choice"',
    )
    assert (
        parse_quiz_random_question_bank(
            mixed,
            base_url=BASE_URL,
            course_id="549",
        )["all_essay"]
        is False
    )

    paginated = html.replace(
        "</table>",
        f'</table><a href="{BASE_URL}/question/bank/viewquestion/view.php?'
        'cmid=30354&amp;qpage=1">2</a>',
    )
    with pytest.raises(MoodleMarkupError, match="paginated"):
        parse_quiz_random_question_bank(
            paginated,
            base_url=BASE_URL,
            course_id="549",
        )

    aria_pagination = html.replace(
        "</table>",
        '</table><nav aria-label="Pagination"><button data-page="next">Next</button></nav>',
    )
    with pytest.raises(MoodleMarkupError, match="paginated"):
        parse_quiz_random_question_bank(
            aria_pagination,
            base_url=BASE_URL,
            course_id="549",
        )


def test_question_statement_requires_same_origin_unique_link_and_matching_essay_form() -> None:
    foreign = fixture("quiz_edit_single_essay.html").replace(
        'href="/question/bank/editquestion/question.php?',
        'href="https://evil.example/question/bank/editquestion/question.php?',
    )
    summary = parse_quiz_question_summary(
        foreign,
        course_id="549",
        cmid=30354,
        base_url=BASE_URL,
    )
    assert "_essay_edit_url" not in summary

    other_question = fixture("question_edit_essay.html").replace(
        'name="id" value="7711"', 'name="id" value="7712"'
    )
    with pytest.raises(MoodleMarkupError, match="another item"):
        parse_quiz_essay_question_edit(
            other_question,
            course_id="549",
            cmid=30354,
            question_id="7711",
        )

    attachment_only = (
        fixture("question_edit_essay.html")
        .replace(
            '<option selected value="monospaced">Текстовый редактор</option>',
            '<option selected value="noinline">Без текста</option>',
        )
        .replace(
            '<option selected value="0">Нет</option>',
            '<option selected value="-1">Без ограничений</option>',
        )
    )
    assert (
        parse_quiz_essay_question_edit(
            attachment_only,
            course_id="549",
            cmid=30354,
            question_id="7711",
        )["answer_transport"]
        == "ESSAY_ATTACHMENT"
    )


def test_participants_parse_stable_ids_roles_groups_and_bounded_next_marker() -> None:
    parsed = parse_participants_page(fixture("participants.html"), BASE_URL, "549")
    assert parsed.has_next is True
    assert parsed.table_present is True
    assert parsed.all_rows_classified is True
    assert len(parsed.members) == 2
    teacher, student = parsed.members
    assert teacher == {
        "user_id": "42",
        "display_name": "Алексей Преподаватель",
        "email": "teacher@example.test",
        "suspended": False,
        "role": "TEACHER",
        "roles": ["TEACHER"],
        "groups": [{"external_id": "9", "name": "2.4 подгруппа А"}],
    }
    assert student["user_id"] == "77"
    assert student["role"] == "STUDENT"
    assert student["groups"][0]["external_id"].startswith("name-")


def test_foreign_logout_and_pagination_links_do_not_count() -> None:
    html = """
    <a href='https://evil.example/login/logout.php'>logout</a>
    <table id='participants'><tbody></tbody></table>
    <a rel='next' href='https://evil.example/user/index.php?id=549&page=1'>next</a>
    """
    assert not has_authenticated_markup(html, BASE_URL)
    assert not parse_participants_page(html, BASE_URL, "549").has_next


def test_participants_link_alone_does_not_escalate_actor_to_teacher() -> None:
    html = "<a href='/user/index.php?id=549'>Участники</a>"
    assert not teacher_controls_present(html, BASE_URL, "549")


def test_mmcs_course_settings_confirm_only_the_matching_course() -> None:
    html = fixture("course_settings_teacher.html")
    assert teacher_controls_present(html, BASE_URL, "549")
    assert not teacher_controls_present(html, BASE_URL, "550")
    assert not teacher_controls_present(
        "<a href='https://evil.example/course/edit.php?id=549'>Настройки</a>",
        BASE_URL,
        "549",
    )


def test_top_format_section_links_are_rebuilt_and_activities_are_merged() -> None:
    main_html = fixture("course_top_main.html")
    links = parse_course_section_links(main_html, BASE_URL, "549", maximum=12)
    assert [(link.section, link.url) for link in links] == [
        (1, f"{BASE_URL}/course/view.php?id=549&section=1"),
        (2, f"{BASE_URL}/course/view.php?id=549&section=2"),
    ]
    main = parse_course_page(main_html, BASE_URL, "549")
    assert all(not section["activities"] for section in main["sections"])
    merged = merge_course_sections(
        main,
        [
            parse_course_page(fixture("course_top_section_1.html"), BASE_URL, "549"),
            parse_course_page(fixture("course_top_section_2.html"), BASE_URL, "549"),
        ],
    )
    assert [section["external_id"] for section in merged["sections"]] == ["10", "20"]
    assert [
        activity["cmid"] for section in merged["sections"] for activity in section["activities"]
    ] == [777, 888]


def test_moodle_52_section_record_links_recover_summary_only_activities() -> None:
    main_html = fixture("course_moodle52_main.html")
    links = parse_course_section_links(main_html, BASE_URL, "549", maximum=12)
    assert [(link.section, link.section_record_id, link.url) for link in links] == [
        (0, 6952, f"{BASE_URL}/course/section.php?id=6952"),
        (10, 6962, f"{BASE_URL}/course/section.php?id=6962"),
    ]

    main = parse_course_page(main_html, BASE_URL, "549")
    assert all(not section["activities"] for section in main["sections"])
    section_page = parse_course_page(
        fixture("course_moodle52_section_6962.html"),
        BASE_URL,
        "549",
        expected_section_record_id=6962,
    )
    merged = merge_course_sections(main, [section_page])
    assert (
        next(section["title"] for section in merged["sections"] if section["external_id"] == "10")
        == "Проверочные (самостоятельные работы)"
    )
    discovered = {
        activity["cmid"]: activity["name"]
        for section in merged["sections"]
        for activity in section["activities"]
    }
    assert discovered[30354] == "Самостоятельная работа №1"
    assert discovered[30355] == "Самостоятельная работа №2"


def test_moodle_52_section_response_requires_exact_course_and_record_evidence() -> None:
    html = fixture("course_moodle52_section_6962.html")
    with pytest.raises(MoodleMarkupError, match="another course"):
        parse_course_page(
            html.replace("course-549", "course-550"),
            BASE_URL,
            "549",
            expected_section_record_id=6962,
        )
    with pytest.raises(MoodleMarkupError, match="another section"):
        parse_course_page(
            html,
            BASE_URL,
            "549",
            expected_section_record_id=9999,
        )
