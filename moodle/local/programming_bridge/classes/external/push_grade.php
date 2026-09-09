<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');
require_once($CFG->libdir . '/gradelib.php');

use context_module;
use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

final class push_grade extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Expected Moodle course id'),
            'cmid' => new external_value(PARAM_INT, 'Mapped Assignment course-module id'),
            'userid' => new external_value(PARAM_INT, 'Moodle user id'),
            'grade' => new external_value(PARAM_FLOAT, 'Raw grade'),
            'comment' => new external_value(PARAM_RAW, 'Teacher feedback', VALUE_DEFAULT, ''),
            'idempotencykey' => new external_value(PARAM_ALPHANUMEXT, 'Globally unique idempotency key'),
        ]);
    }

    public static function execute(
        int $courseid,
        int $cmid,
        int $userid,
        float $grade,
        string $comment,
        string $idempotencykey
    ): array {
        global $DB;
        $params = self::validate_parameters(self::execute_parameters(), compact(
            'courseid', 'cmid', 'userid', 'grade', 'comment', 'idempotencykey'
        ));
        $cm = get_coursemodule_from_id('', $params['cmid'], 0, false, MUST_EXIST);
        if ((int)$cm->course !== (int)$params['courseid']) {
            throw new \invalid_parameter_exception('Mapped course module belongs to another course');
        }
        $context = context_module::instance($cm->id);
        self::validate_context($context);
        $coursecontext = context_course::instance($cm->course);
        require_capability('local/programming_bridge:pushgrade', $coursecontext);
        if (!is_enrolled($coursecontext, $params['userid'], '', true)) {
            throw new \invalid_parameter_exception('The target user is not actively enrolled in this course');
        }

        if ($cm->modname !== 'assign') {
            throw new \moodle_exception('Only mod_assign grade mappings are supported by bridge v1');
        }
        $payloadhash = hash('sha256', json_encode($params, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR));
        $receipt = $DB->get_record('local_prgbridge_receipt', ['idempotencykey' => $params['idempotencykey']]);
        if ($receipt) {
            if (!hash_equals($receipt->payloadhash, $payloadhash)) {
                throw new \moodle_exception('Idempotency key was already used with a different payload');
            }
            return json_decode($receipt->resultjson, true, 512, JSON_THROW_ON_ERROR);
        }

        $instance = $DB->get_record('assign', ['id' => $cm->instance], '*', MUST_EXIST);
        if ($params['grade'] < 0 || $params['grade'] > (float)$instance->grade) {
            throw new \invalid_parameter_exception('Grade is outside the Assignment grade range');
        }
        $now = time();
        $status = grade_update(
            'mod/assign',
            (int)$cm->course,
            'mod',
            'assign',
            (int)$cm->instance,
            0,
            [[
                'userid' => $params['userid'],
                'rawgrade' => $params['grade'],
                'feedback' => clean_text($params['comment'], FORMAT_PLAIN),
                'feedbackformat' => FORMAT_PLAIN,
                'datesubmitted' => $now,
                'dategraded' => $now,
            ]]
        );
        if ($status !== GRADE_UPDATE_OK) {
            throw new \moodle_exception('Moodle grade_update failed with status ' . $status);
        }

        $result = [
            'status' => 'DELIVERED',
            'receipt' => hash('sha256', $params['idempotencykey'] . ':' . $payloadhash),
            'timecreated' => $now,
        ];
        $DB->insert_record('local_prgbridge_receipt', (object)[
            'idempotencykey' => $params['idempotencykey'],
            'operation' => 'push_grade',
            'payloadhash' => $payloadhash,
            'resultjson' => json_encode($result, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR),
            'timecreated' => $now,
        ]);
        return $result;
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'status' => new external_value(PARAM_ALPHA, 'Delivery status'),
            'receipt' => new external_value(PARAM_ALPHANUM, 'Receipt hash'),
            'timecreated' => new external_value(PARAM_INT, 'Moodle timestamp'),
        ]);
    }
}
