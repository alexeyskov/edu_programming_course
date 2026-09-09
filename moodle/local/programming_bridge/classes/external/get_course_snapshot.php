<?php

namespace local_programming_bridge\external;

defined('MOODLE_INTERNAL') || die();
require_once($CFG->libdir . '/externallib.php');

use context_course;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

final class get_course_snapshot extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'courseid' => new external_value(PARAM_INT, 'Moodle course id'),
        ]);
    }

    public static function execute(int $courseid): array {
        global $DB;
        ['courseid' => $courseid] = self::validate_parameters(self::execute_parameters(), [
            'courseid' => $courseid,
        ]);
        $course = $DB->get_record('course', ['id' => $courseid], '*', MUST_EXIST);
        $context = context_course::instance($courseid);
        self::validate_context($context);
        require_capability('local/programming_bridge:use', $context);

        $modinfo = get_fast_modinfo($course);
        $cmsbysection = [];
        foreach ($modinfo->get_cms() as $cm) {
            $activity = [
                'cmid' => (int)$cm->id,
                'instance_id' => (int)$cm->instance,
                'module' => $cm->modname,
                'name' => $cm->name,
                'visible' => (bool)$cm->visible,
                'uservisible' => (bool)$cm->uservisible,
                'url' => $cm->url ? $cm->url->out(false) : null,
            ];
            if ($cm->modname === 'assign') {
                $assign = $DB->get_record(
                    'assign',
                    ['id' => $cm->instance],
                    'id,allowsubmissionsfromdate,duedate,cutoffdate,grade',
                    IGNORE_MISSING
                );
                if ($assign) {
                    $activity['opens_at'] = (int)$assign->allowsubmissionsfromdate;
                    $activity['due_at'] = (int)$assign->duedate;
                    $activity['cutoff_at'] = (int)$assign->cutoffdate;
                    $activity['grade_max'] = (float)$assign->grade;
                    $activity['grade_type'] = (float)$assign->grade > 0
                        ? 'point'
                        : ((float)$assign->grade < 0 ? 'scale' : 'none');
                }
            }
            $cmsbysection[(int)$cm->sectionnum][] = $activity;
        }
        $sections = [];
        foreach ($modinfo->get_section_info_all() as $section) {
            $sections[] = [
                'id' => (int)$section->id,
                'number' => (int)$section->section,
                'name' => get_section_name($course, $section),
                'visible' => (bool)$section->visible,
                'activities' => $cmsbysection[(int)$section->section] ?? [],
            ];
        }
        $payload = [
            'course' => [
                'id' => (int)$course->id,
                'shortname' => $course->shortname,
                'fullname' => $course->fullname,
                'startdate' => (int)$course->startdate,
                'enddate' => (int)$course->enddate,
                'visible' => (bool)$course->visible,
            ],
            'sections' => $sections,
        ];
        $json = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
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
