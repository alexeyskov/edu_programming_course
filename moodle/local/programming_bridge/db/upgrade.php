<?php

defined('MOODLE_INTERNAL') || die();

function xmldb_local_programming_bridge_upgrade(int $oldversion): bool {
    global $DB;

    $dbman = $DB->get_manager();
    if ($oldversion < 2026082401) {
        $table = new xmldb_table('local_prgbridge_checkpoint');

        $idempotencyfield = new xmldb_field(
            'idempotencykey',
            XMLDB_TYPE_CHAR,
            '128',
            null,
            false,
            null,
            null,
            'id'
        );
        if (!$dbman->field_exists($table, $idempotencyfield)) {
            $dbman->add_field($table, $idempotencyfield);
        }

        $manifestfield = new xmldb_field(
            'manifestjson',
            XMLDB_TYPE_TEXT,
            null,
            null,
            false,
            null,
            null,
            'snapshotsha256'
        );
        if (!$dbman->field_exists($table, $manifestfield)) {
            $dbman->add_field($table, $manifestfield);
        }

        $records = $DB->get_recordset('local_prgbridge_checkpoint');
        foreach ($records as $record) {
            $record->idempotencykey = 'legacy-' . $record->id;
            $record->manifestjson = '[]';
            $DB->update_record('local_prgbridge_checkpoint', $record);
        }
        $records->close();

        $idempotencyfield->set_attributes(
            XMLDB_TYPE_CHAR,
            '128',
            null,
            true,
            null,
            null,
            'id'
        );
        $dbman->change_field_notnull($table, $idempotencyfield);
        $manifestfield->set_attributes(
            XMLDB_TYPE_TEXT,
            null,
            null,
            true,
            null,
            null,
            'snapshotsha256'
        );
        $dbman->change_field_notnull($table, $manifestfield);

        $index = new xmldb_index(
            'checkpointidempotency_uix',
            XMLDB_INDEX_UNIQUE,
            ['idempotencykey']
        );
        if (!$dbman->index_exists($table, $index)) {
            $dbman->add_index($table, $index);
        }

        upgrade_plugin_savepoint(true, 2026082401, 'local', 'programming_bridge');
    }

    if ($oldversion < 2026082404) {
        $table = new xmldb_table('local_prgbridge_checkpoint');
        $fields = [
            new xmldb_field(
                'eventchainhead', XMLDB_TYPE_CHAR, '64', null, true, null, '', 'snapshotsha256'
            ),
            new xmldb_field(
                'epoch', XMLDB_TYPE_INTEGER, '5', null, true, null, '1', 'eventchainhead'
            ),
            new xmldb_field(
                'workspacerevision', XMLDB_TYPE_INTEGER, '20', null, true, null, '0', 'epoch'
            ),
        ];
        foreach ($fields as $field) {
            if (!$dbman->field_exists($table, $field)) {
                $dbman->add_field($table, $field);
            }
        }
        upgrade_plugin_savepoint(true, 2026082404, 'local', 'programming_bridge');
    }

    if ($oldversion < 2026082405) {
        $table = new xmldb_table('local_prgbridge_task');
        if (!$dbman->table_exists($table)) {
            $table->add_field('id', XMLDB_TYPE_INTEGER, '10', null, true, XMLDB_SEQUENCE);
            $table->add_field('courseid', XMLDB_TYPE_INTEGER, '10', null, true);
            $table->add_field('taskref', XMLDB_TYPE_CHAR, '128', null, true);
            $table->add_field('versionnum', XMLDB_TYPE_INTEGER, '10', null, true);
            $table->add_field('contenthash', XMLDB_TYPE_CHAR, '64', null, true);
            $table->add_field('definitionhash', XMLDB_TYPE_CHAR, '64', null, true);
            $table->add_field('definitionjson', XMLDB_TYPE_TEXT, null, null, true);
            $table->add_field('status', XMLDB_TYPE_CHAR, '16', null, true);
            $table->add_field('timecreated', XMLDB_TYPE_INTEGER, '10', null, true);
            $table->add_field('timemodified', XMLDB_TYPE_INTEGER, '10', null, true);
            $table->add_key('primary', XMLDB_KEY_PRIMARY, ['id']);
            $table->add_key('course_fk', XMLDB_KEY_FOREIGN, ['courseid'], 'course', ['id']);
            $table->add_index(
                'coursetaskversion_uix', XMLDB_INDEX_UNIQUE, ['courseid', 'taskref', 'versionnum']
            );
            $table->add_index(
                'coursemodified_ix', XMLDB_INDEX_NOTUNIQUE, ['courseid', 'timemodified']
            );
            $dbman->create_table($table);
        }
        upgrade_plugin_savepoint(true, 2026082405, 'local', 'programming_bridge');
    }

    return true;
}
