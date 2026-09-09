<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');

use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

/** Returns the connector-owned task mirror; native question-bank editing is intentionally separate. */
final class get_task_bank_snapshot extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Moodle course id'),
        ]);
    }

    public static function execute(int $courseid): array {
        global $DB;
        ['courseid' => $courseid] = self::validate_parameters(
            self::execute_parameters(),
            ['courseid' => $courseid]
        );
        $context = context_course::instance($courseid);
        self::validate_context($context);
        require_capability('local/programming_bridge:synctaskbank', $context);
        $records = $DB->get_records(
            'local_prgbridge_task',
            ['courseid' => $courseid],
            'taskref ASC, versionnum ASC'
        );
        $tasks = [];
        foreach ($records as $record) {
            $tasks[] = [
                'mirror_id' => (int)$record->id,
                'task_ref' => $record->taskref,
                'version' => (int)$record->versionnum,
                'content_hash' => $record->contenthash,
                'definition_hash' => $record->definitionhash,
                'definition' => json_decode($record->definitionjson, true, 512, JSON_THROW_ON_ERROR),
                'status' => $record->status,
                'modified_at' => (int)$record->timemodified,
            ];
        }
        $payload = json_encode(
            ['course_id' => $courseid, 'tasks' => $tasks],
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES |
                JSON_UNESCAPED_LINE_TERMINATORS | JSON_THROW_ON_ERROR
        );
        return ['revision' => hash('sha256', $payload), 'payload' => $payload];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'revision' => new external_value(PARAM_ALPHANUM, 'Task-bank mirror revision'),
            'payload' => new external_value(PARAM_RAW, 'UTF-8 JSON projection'),
        ]);
    }
}
