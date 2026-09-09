<?php

namespace local_programming_bridge\privacy;

defined('MOODLE_INTERNAL') || die();

use context;
use context_course;
use core_privacy\local\metadata\collection;
use core_privacy\local\request\approved_contextlist;
use core_privacy\local\request\contextlist;
use core_privacy\local\request\writer;

final class provider implements
    \core_privacy\local\metadata\provider,
    \core_privacy\local\request\plugin\provider {

    public static function get_metadata(collection $collection): collection {
        $collection->add_database_table('local_prgbridge_checkpoint', [
            'userid' => 'privacy:metadata:local_prgbridge_checkpoint:userid',
            'attemptref' => 'privacy:metadata:local_prgbridge_checkpoint:attemptref',
            'snapshotref' => 'privacy:metadata:local_prgbridge_checkpoint:snapshotref',
            'snapshotsha256' => 'privacy:metadata:local_prgbridge_checkpoint:snapshotsha256',
            'eventchainhead' => 'privacy:metadata:local_prgbridge_checkpoint:eventchainhead',
            'epoch' => 'privacy:metadata:local_prgbridge_checkpoint:epoch',
            'workspacerevision' => 'privacy:metadata:local_prgbridge_checkpoint:workspacerevision',
            'manifestjson' => 'privacy:metadata:local_prgbridge_checkpoint:manifestjson',
            'reason' => 'privacy:metadata:local_prgbridge_checkpoint:reason',
            'timecreated' => 'privacy:metadata:local_prgbridge_checkpoint:timecreated',
        ], 'privacy:metadata:local_prgbridge_checkpoint');
        return $collection;
    }

    public static function get_contexts_for_userid(int $userid): contextlist {
        $contextlist = new contextlist();
        $sql = 'SELECT ctx.id FROM {context} ctx
                  JOIN {local_prgbridge_checkpoint} cp ON cp.courseid = ctx.instanceid
                 WHERE ctx.contextlevel = :contextlevel AND cp.userid = :userid';
        $contextlist->add_from_sql($sql, ['contextlevel' => CONTEXT_COURSE, 'userid' => $userid]);
        return $contextlist;
    }

    public static function export_user_data(approved_contextlist $contextlist): void {
        global $DB;
        foreach ($contextlist->get_contexts() as $context) {
            if (!$context instanceof context_course) {
                continue;
            }
            $records = $DB->get_records('local_prgbridge_checkpoint', [
                'courseid' => $context->instanceid,
                'userid' => $contextlist->get_user()->id,
            ]);
            writer::with_context($context)->export_data(
                [get_string('pluginname', 'local_programming_bridge')],
                (object)['checkpoints' => array_values($records)]
            );
        }
    }

    public static function delete_data_for_all_users_in_context(context $context): void {
        global $DB;
        if ($context instanceof context_course) {
            $DB->delete_records('local_prgbridge_checkpoint', ['courseid' => $context->instanceid]);
        }
    }

    public static function delete_data_for_user(approved_contextlist $contextlist): void {
        global $DB;
        foreach ($contextlist->get_contexts() as $context) {
            if ($context instanceof context_course) {
                $DB->delete_records('local_prgbridge_checkpoint', [
                    'courseid' => $context->instanceid,
                    'userid' => $contextlist->get_user()->id,
                ]);
            }
        }
    }
}
