<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');

use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

final class store_checkpoint extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Moodle course id'),
            'userid' => new external_value(PARAM_INT, 'Moodle user id'),
            'attemptref' => new external_value(PARAM_ALPHANUMEXT, 'Opaque platform attempt id'),
            'snapshotref' => new external_value(PARAM_ALPHANUMEXT, 'Opaque platform snapshot id'),
            'snapshotsha256' => new external_value(PARAM_ALPHANUM, 'Snapshot SHA-256'),
            'eventchainhead' => new external_value(PARAM_ALPHANUM, 'Edit event chain head or empty'),
            'epoch' => new external_value(PARAM_INT, 'Attempt edit-history epoch'),
            'workspacerevision' => new external_value(PARAM_INT, 'Workspace revision'),
            'reason' => new external_value(PARAM_ALPHAEXT, 'Checkpoint reason'),
            'manifestjson' => new external_value(PARAM_RAW, 'Canonical UTF-8 source manifest JSON'),
            'idempotencykey' => new external_value(PARAM_ALPHANUMEXT, 'Globally unique idempotency key'),
        ]);
    }

    public static function execute(
        int $courseid,
        int $userid,
        string $attemptref,
        string $snapshotref,
        string $snapshotsha256,
        string $eventchainhead,
        int $epoch,
        int $workspacerevision,
        string $reason,
        string $manifestjson,
        string $idempotencykey
    ): array {
        global $DB;
        $params = self::validate_parameters(self::execute_parameters(), compact(
            'courseid', 'userid', 'attemptref', 'snapshotref', 'snapshotsha256',
            'eventchainhead', 'epoch', 'workspacerevision', 'reason', 'manifestjson',
            'idempotencykey'
        ));
        if (!preg_match('/^[a-f0-9]{64}$/i', $params['snapshotsha256'])) {
            throw new \invalid_parameter_exception('snapshotsha256 must contain 64 hexadecimal characters');
        }
        $params['snapshotsha256'] = strtolower($params['snapshotsha256']);
        if (
            $params['eventchainhead'] !== '' &&
            !preg_match('/^[a-f0-9]{64}$/i', $params['eventchainhead'])
        ) {
            throw new \invalid_parameter_exception(
                'eventchainhead must be empty or contain 64 hexadecimal characters'
            );
        }
        if ($params['epoch'] < 1 || $params['workspacerevision'] < 0) {
            throw new \invalid_parameter_exception('epoch/revision are outside the valid range');
        }
        $params['eventchainhead'] = strtolower($params['eventchainhead']);
        if (strlen($params['manifestjson']) > 4 * 1024 * 1024) {
            throw new \invalid_parameter_exception('manifestjson exceeds the 4 MiB recovery limit');
        }
        try {
            $manifest = json_decode($params['manifestjson'], true, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException $exception) {
            throw new \invalid_parameter_exception('manifestjson must be valid UTF-8 JSON');
        }
        if (!is_array($manifest)) {
            throw new \invalid_parameter_exception('manifestjson must contain a JSON array or object');
        }
        // The sender defines canonical bytes; cross-runtime JSON re-encoding is not
        // byte-stable for every valid floating-point value. Verify the exact bytes.
        if (!hash_equals($params['snapshotsha256'], hash('sha256', $params['manifestjson']))) {
            throw new \invalid_parameter_exception('snapshotsha256 does not match manifestjson');
        }
        $context = context_course::instance($params['courseid']);
        self::validate_context($context);
        require_capability('local/programming_bridge:storecheckpoint', $context);
        if (!is_enrolled($context, $params['userid'], '', true)) {
            throw new \invalid_parameter_exception('The target user is not actively enrolled in this course');
        }

        $record = $DB->get_record(
            'local_prgbridge_checkpoint',
            ['idempotencykey' => $params['idempotencykey']]
        );
        if (!$record) {
            $record = $DB->get_record('local_prgbridge_checkpoint', [
                'attemptref' => $params['attemptref'],
                'snapshotref' => $params['snapshotref'],
            ]);
        }
        if (!$record) {
            $record = (object)($params + ['timecreated' => time()]);
            $record->id = $DB->insert_record('local_prgbridge_checkpoint', $record);
        } else if (
            strpos($record->idempotencykey, 'legacy-') === 0 &&
            $record->manifestjson === '[]' &&
            hash_equals($record->snapshotsha256, $params['snapshotsha256'])
        ) {
            $record->idempotencykey = $params['idempotencykey'];
            $record->manifestjson = $params['manifestjson'];
            $record->reason = $params['reason'];
            $record->eventchainhead = $params['eventchainhead'];
            $record->epoch = $params['epoch'];
            $record->workspacerevision = $params['workspacerevision'];
            $DB->update_record('local_prgbridge_checkpoint', $record);
        } else if (
            !hash_equals($record->snapshotsha256, $params['snapshotsha256']) ||
            !hash_equals($record->manifestjson, $params['manifestjson']) ||
            (int)$record->courseid !== (int)$params['courseid'] ||
            (int)$record->userid !== (int)$params['userid'] ||
            $record->attemptref !== $params['attemptref'] ||
            $record->snapshotref !== $params['snapshotref'] ||
            !hash_equals($record->eventchainhead, $params['eventchainhead']) ||
            (int)$record->epoch !== (int)$params['epoch'] ||
            (int)$record->workspacerevision !== (int)$params['workspacerevision']
        ) {
            throw new \moodle_exception('Checkpoint idempotency reference has another payload');
        }
        return [
            'status' => 'STORED',
            'checkpointid' => (int)$record->id,
            'timecreated' => (int)$record->timecreated,
            'manifestbytes' => strlen($record->manifestjson),
        ];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'status' => new external_value(PARAM_ALPHA, 'Storage status'),
            'checkpointid' => new external_value(PARAM_INT, 'Moodle checkpoint id'),
            'timecreated' => new external_value(PARAM_INT, 'Moodle timestamp'),
            'manifestbytes' => new external_value(PARAM_INT, 'Stored canonical manifest size'),
        ]);
    }
}
