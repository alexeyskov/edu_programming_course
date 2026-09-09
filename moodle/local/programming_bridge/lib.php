<?php

defined('MOODLE_INTERNAL') || die();

/**
 * Adds a delegated-launch link to course navigation.
 *
 * @param navigation_node $navigation
 * @param stdClass $course
 * @param context_course $context
 */
function local_programming_bridge_extend_navigation_course(
    navigation_node $navigation,
    stdClass $course,
    context_course $context
): void {
    if (!has_capability('local/programming_bridge:use', $context)) {
        return;
    }
    if (!get_config('local_programming_bridge', 'platformurl')) {
        return;
    }
    $url = new moodle_url('/local/programming_bridge/launch.php', [
        'courseid' => $course->id,
        'sesskey' => sesskey(),
    ]);
    $navigation->add(
        get_string('openplatform', 'local_programming_bridge'),
        $url,
        navigation_node::TYPE_CUSTOM,
        null,
        'local_programming_bridge',
        new pix_icon('i/code', '')
    );
}

