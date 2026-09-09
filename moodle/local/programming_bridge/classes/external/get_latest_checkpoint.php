<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');

use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

/** Returns the newest verified recovery checkpoint for one opaque attempt. */
final class get_latest_checkpoint extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Moodle course id'),
            'userid' => new external_value(PARAM_INT, 'Moodle user id'),
            'attemptref' => new external_value(PARAM_ALPHANUMEXT, 'Opaque platform attempt id'),
        ]);
    }

    public static function execute(int $courseid, int $userid, string $attemptref): array {
        global $DB;
        $params = self::validate_parameters(
            self::execute_parameters(),
            compact('courseid', 'userid', 'attemptref')
        );
        $context = context_course::instance($params['courseid']);
        self::validate_context($context);
        require_capability('local/programming_bridge:storecheckpoint', $context);

        $records = $DB->get_records(
            'local_prgbridge_checkpoint',
            [
                'courseid' => $params['courseid'],
                'userid' => $params['userid'],
                'attemptref' => $params['attemptref'],
            ],
            'timecreated DESC, id DESC',
            '*',
            0,
            1
        );
        $record = $records ? reset($records) : false;
        if (!$record) {
            return [
                'status' => 'NOT_FOUND',
                'courseid' => 0,
                'userid' => 0,
                'attemptref' => '',
                'snapshotref' => '',
                'snapshotsha256' => '',
                'eventchainhead' => '',
                'epoch' => 0,
                'workspacerevision' => 0,
                'reason' => '',
                'manifestjson' => '',
                'timecreated' => 0,
            ];
        }
        $actualhash = hash('sha256', $record->manifestjson);
        if (!hash_equals($record->snapshotsha256, $actualhash)) {
            throw new \moodle_exception('storedcheckpointcorrupt', 'local_programming_bridge');
        }
        return [
            'status' => 'FOUND',
            'courseid' => (int)$record->courseid,
            'userid' => (int)$record->userid,
            'attemptref' => $record->attemptref,
            'snapshotref' => $record->snapshotref,
            'snapshotsha256' => $record->snapshotsha256,
            'eventchainhead' => $record->eventchainhead,
            'epoch' => (int)$record->epoch,
            'workspacerevision' => (int)$record->workspacerevision,
            'reason' => $record->reason,
            'manifestjson' => $record->manifestjson,
            'timecreated' => (int)$record->timecreated,
        ];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'status' => new external_value(PARAM_ALPHAEXT, 'FOUND or NOT_FOUND'),
            'courseid' => new external_value(PARAM_INT, 'Moodle course id or zero'),
            'userid' => new external_value(PARAM_INT, 'Moodle user id or zero'),
            'attemptref' => new external_value(PARAM_ALPHANUMEXT, 'Opaque attempt id or empty'),
            'snapshotref' => new external_value(PARAM_ALPHANUMEXT, 'Opaque snapshot id or empty'),
            'snapshotsha256' => new external_value(PARAM_ALPHANUM, 'Verified SHA-256 or empty'),
            'eventchainhead' => new external_value(PARAM_ALPHANUM, 'Edit event chain head or empty'),
            'epoch' => new external_value(PARAM_INT, 'Attempt history epoch or zero'),
            'workspacerevision' => new external_value(PARAM_INT, 'Workspace revision'),
            'reason' => new external_value(PARAM_ALPHAEXT, 'Checkpoint reason or empty'),
            'manifestjson' => new external_value(PARAM_RAW, 'Canonical source manifest or empty'),
            'timecreated' => new external_value(PARAM_INT, 'Moodle timestamp or zero'),
        ]);
    }
}
