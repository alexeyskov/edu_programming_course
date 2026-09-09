<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');

use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

/** Stores an immutable, connector-owned mirror of a programming task version. */
final class upsert_task_definition extends external_api {
    private const MAX_DEFINITION_BYTES = 4 * 1024 * 1024;

    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Moodle course id'),
            'taskref' => new external_value(PARAM_ALPHANUMEXT, 'Stable platform task id'),
            'versionnum' => new external_value(PARAM_INT, 'Immutable task version number'),
            'contenthash' => new external_value(PARAM_ALPHANUM, 'Semantic task content hash'),
            'definitionhash' => new external_value(PARAM_ALPHANUM, 'Definition JSON SHA-256'),
            'definitionjson' => new external_value(PARAM_RAW, 'Canonical UTF-8 task definition'),
            'status' => new external_value(PARAM_ALPHA, 'PUBLISHED or ARCHIVED'),
            'idempotencykey' => new external_value(PARAM_ALPHANUMEXT, 'Idempotency key'),
        ]);
    }

    public static function execute(
        int $courseid,
        string $taskref,
        int $versionnum,
        string $contenthash,
        string $definitionhash,
        string $definitionjson,
        string $status,
        string $idempotencykey
    ): array {
        global $DB;
        $params = self::validate_parameters(self::execute_parameters(), compact(
            'courseid', 'taskref', 'versionnum', 'contenthash', 'definitionhash',
            'definitionjson', 'status', 'idempotencykey'
        ));
        if ($params['versionnum'] < 1) {
            throw new \invalid_parameter_exception('versionnum must be positive');
        }
        foreach (['contenthash', 'definitionhash'] as $field) {
            if (!preg_match('/^[a-f0-9]{64}$/i', $params[$field])) {
                throw new \invalid_parameter_exception($field . ' must be a SHA-256 hex value');
            }
            $params[$field] = strtolower($params[$field]);
        }
        $params['status'] = strtoupper($params['status']);
        if (!in_array($params['status'], ['PUBLISHED', 'ARCHIVED'], true)) {
            throw new \invalid_parameter_exception('Unsupported task mirror status');
        }
        if (strlen($params['definitionjson']) > self::MAX_DEFINITION_BYTES) {
            throw new \invalid_parameter_exception('definitionjson exceeds the 4 MiB limit');
        }
        try {
            $definition = json_decode($params['definitionjson'], true, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException $exception) {
            throw new \invalid_parameter_exception('definitionjson must be valid UTF-8 JSON');
        }
        if (!is_array($definition)) {
            throw new \invalid_parameter_exception('definitionjson must contain a JSON object');
        }
        // The platform owns canonical serialization. Re-encoding here would be unsafe:
        // PHP and Python use different valid spellings for some floating-point values.
        // The digest below authenticates the exact UTF-8 bytes that Moodle stores.
        if (!hash_equals($params['definitionhash'], hash('sha256', $params['definitionjson']))) {
            throw new \invalid_parameter_exception('definitionhash does not match definitionjson');
        }
        if (
            !isset($definition['content_hash']) ||
            !is_string($definition['content_hash']) ||
            !hash_equals($params['contenthash'], strtolower($definition['content_hash']))
        ) {
            throw new \invalid_parameter_exception('contenthash does not match task definition');
        }

        $context = context_course::instance($params['courseid']);
        self::validate_context($context);
        require_capability('local/programming_bridge:synctaskbank', $context);

        $payloadhash = hash(
            'sha256',
            json_encode($params, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR)
        );
        $receipt = $DB->get_record(
            'local_prgbridge_receipt',
            ['idempotencykey' => $params['idempotencykey']]
        );
        if ($receipt) {
            if (!hash_equals($receipt->payloadhash, $payloadhash)) {
                throw new \moodle_exception('Task idempotency key has another payload');
            }
            return json_decode($receipt->resultjson, true, 512, JSON_THROW_ON_ERROR);
        }

        $record = $DB->get_record('local_prgbridge_task', [
            'courseid' => $params['courseid'],
            'taskref' => $params['taskref'],
            'versionnum' => $params['versionnum'],
        ]);
        $now = time();
        if (!$record) {
            $record = (object)[
                'courseid' => $params['courseid'],
                'taskref' => $params['taskref'],
                'versionnum' => $params['versionnum'],
                'contenthash' => $params['contenthash'],
                'definitionhash' => $params['definitionhash'],
                'definitionjson' => $params['definitionjson'],
                'status' => $params['status'],
                'timecreated' => $now,
                'timemodified' => $now,
            ];
            $record->id = $DB->insert_record('local_prgbridge_task', $record);
        } else if (
            !hash_equals($record->contenthash, $params['contenthash']) ||
            !hash_equals($record->definitionhash, $params['definitionhash']) ||
            !hash_equals($record->definitionjson, $params['definitionjson'])
        ) {
            throw new \moodle_exception('Immutable task version already has another payload');
        } else if ($record->status !== $params['status']) {
            $record->status = $params['status'];
            $record->timemodified = $now;
            $DB->update_record('local_prgbridge_task', $record);
        }

        $result = [
            'status' => 'MIRRORED',
            'mirrorid' => (int)$record->id,
            'timecreated' => (int)$record->timecreated,
            'timemodified' => (int)$record->timemodified,
        ];
        $DB->insert_record('local_prgbridge_receipt', (object)[
            'idempotencykey' => $params['idempotencykey'],
            'operation' => 'upsert_task',
            'payloadhash' => $payloadhash,
            'resultjson' => json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR),
            'timecreated' => $now,
        ]);
        return $result;
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'status' => new external_value(PARAM_ALPHA, 'Mirror status'),
            'mirrorid' => new external_value(PARAM_INT, 'Moodle mirror id'),
            'timecreated' => new external_value(PARAM_INT, 'Creation timestamp'),
            'timemodified' => new external_value(PARAM_INT, 'Modification timestamp'),
        ]);
    }
}
