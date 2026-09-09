<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');

use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

final class get_membership_snapshot extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Moodle course id'),
        ]);
    }

    public static function execute(int $courseid): array {
        ['courseid' => $courseid] = self::validate_parameters(self::execute_parameters(), [
            'courseid' => $courseid,
        ]);
        $context = context_course::instance($courseid);
        self::validate_context($context);
        require_capability('moodle/course:viewparticipants', $context);

        $members = [];
        $users = get_enrolled_users($context, '', 0, 'u.id,u.firstname,u.lastname,u.username,u.suspended');
        foreach ($users as $user) {
            $groups = groups_get_all_groups($courseid, $user->id, 0, 'g.id,g.name');
            $mappedrole = has_capability('moodle/course:update', $context, $user->id)
                || has_capability('moodle/grade:edit', $context, $user->id)
                ? 'TEACHER'
                : 'STUDENT';
            $members[] = [
                'user_id' => (int)$user->id,
                'username' => $user->username,
                'display_name' => fullname($user),
                'role' => $mappedrole,
                'suspended' => (bool)$user->suspended || !is_enrolled($context, $user->id, '', true),
                'groups' => array_values(array_map(static fn($group) => [
                    'id' => (int)$group->id,
                    'name' => $group->name,
                ], $groups ?: [])),
            ];
        }
        usort($members, static fn(array $left, array $right): int =>
            [$left['display_name'], $left['user_id']] <=> [$right['display_name'], $right['user_id']]
        );
        $json = json_encode(
            ['course_id' => $courseid, 'members' => $members],
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR
        );
        return ['schema_version' => 1, 'revision' => hash('sha256', $json), 'payload' => $json];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'schema_version' => new external_value(PARAM_INT, 'Schema version'),
            'revision' => new external_value(PARAM_ALPHANUM, 'Content revision'),
            'payload' => new external_value(PARAM_RAW, 'UTF-8 JSON payload'),
        ]);
    }
}
