<?php

require_once(__DIR__ . '/../../../config.php');

$courseid = optional_param('courseid', 0, PARAM_INT);
$state = optional_param('state', '', PARAM_ALPHANUMEXT);
$course = $courseid ? get_course($courseid) : null;
$context = $course ? context_course::instance($courseid) : context_system::instance();

require_login($course);
if ($course) {
    require_capability('local/programming_bridge:use', $context);
}

// A platform-initiated login cannot know Moodle's sesskey. After require_login()
// returns from Moodle authentication, keep the user on this trusted origin for
// one explicit confirmation. Course-navigation launches already carry a
// sesskey and skip this screen.
if (!confirm_sesskey()) {
    $continueparams = [
        'courseid' => $courseid,
        'sesskey' => sesskey(),
    ];
    if ($state !== '') {
        $continueparams['state'] = $state;
    }
    $PAGE->set_context($context);
    $PAGE->set_url(new moodle_url('/local/programming_bridge/launch.php', [
        'courseid' => $courseid,
        'state' => $state,
    ]));
    $PAGE->set_title(get_string('pluginname', 'local_programming_bridge'));
    $PAGE->set_heading(get_string('pluginname', 'local_programming_bridge'));

    echo $OUTPUT->header();
    echo $OUTPUT->notification(
        get_string('launchconfirm', 'local_programming_bridge'),
        \core\output\notification::NOTIFY_INFO
    );
    echo $OUTPUT->single_button(
        new moodle_url('/local/programming_bridge/launch.php', $continueparams),
        get_string('continue'),
        'get',
        ['primary' => true]
    );
    echo $OUTPUT->footer();
    exit;
}

$platformurl = rtrim((string)get_config('local_programming_bridge', 'platformurl'), '/');
$sharedsecret = (string)get_config('local_programming_bridge', 'sharedsecret');
if ($platformurl === '' || strlen($sharedsecret) < 32) {
    throw new moodle_exception('missingconfiguration', 'local_programming_bridge');
}
$parsed = parse_url($platformurl);
if (!$parsed || ($parsed['scheme'] ?? '') !== 'https' || empty($parsed['host'])) {
    throw new moodle_exception('invalidreturnurl', 'local_programming_bridge');
}

$mappedrole = 'STUDENT';
if ($course && (has_capability('moodle/course:update', $context) || has_capability('moodle/grade:edit', $context))) {
    $mappedrole = 'TEACHER';
}
$now = time();
$payload = [
    'iss' => (new moodle_url('/'))->out(false),
    'sub' => (string)$USER->id,
    'aud' => $platformurl,
    'iat' => $now,
    'exp' => $now + 60,
    'nonce' => bin2hex(random_bytes(16)),
    'display_name' => fullname($USER),
    'role' => $mappedrole,
    'course_id' => $courseid ?: null,
];
if ($state !== '') {
    $payload['state'] = $state;
}
$body = rtrim(strtr(base64_encode(json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR)), '+/', '-_'), '=');
$signature = rtrim(strtr(base64_encode(hash_hmac('sha256', $body, $sharedsecret, true)), '+/', '-_'), '=');
$assertion = $body . '.' . $signature;

$callback = $platformurl . '/api/v1/auth/moodle/callback';
$PAGE->set_context($context);
$PAGE->set_url(new moodle_url('/local/programming_bridge/launch.php', ['courseid' => $courseid]));
$PAGE->set_title(get_string('pluginname', 'local_programming_bridge'));
$PAGE->set_pagelayout('embedded');

echo $OUTPUT->header();
echo html_writer::start_tag('form', [
    'id' => 'programming-bridge-launch',
    'method' => 'post',
    'action' => $callback,
]);
echo html_writer::empty_tag('input', [
    'type' => 'hidden',
    'name' => 'assertion',
    'value' => $assertion,
]);
if ($state !== '') {
    echo html_writer::empty_tag('input', [
        'type' => 'hidden',
        'name' => 'state',
        'value' => $state,
    ]);
}
echo html_writer::tag('noscript', html_writer::empty_tag('input', [
    'type' => 'submit',
    'value' => get_string('continue'),
]));
echo html_writer::end_tag('form');
$PAGE->requires->js_init_code("document.getElementById('programming-bridge-launch').submit();");
echo $OUTPUT->footer();
