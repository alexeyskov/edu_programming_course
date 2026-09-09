<?php

defined('MOODLE_INTERNAL') || die();

$functions = [
    'local_programming_bridge_get_course_snapshot' => [
        'classname' => 'local_programming_bridge\\external\\get_course_snapshot',
        'description' => 'Returns a versioned course, section and activity projection.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'local/programming_bridge:use',
    ],
    'local_programming_bridge_get_membership_snapshot' => [
        'classname' => 'local_programming_bridge\\external\\get_membership_snapshot',
        'description' => 'Returns enrolled users, mapped roles and groups for a course.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'moodle/course:viewparticipants',
    ],
    'local_programming_bridge_push_grade' => [
        'classname' => 'local_programming_bridge\\external\\push_grade',
        'description' => 'Idempotently updates the gradebook item of a mapped Assignment.',
        'type' => 'write',
        'ajax' => false,
        'capabilities' => 'local/programming_bridge:pushgrade',
    ],
    'local_programming_bridge_store_checkpoint' => [
        'classname' => 'local_programming_bridge\\external\\store_checkpoint',
        'description' => 'Idempotently stores and verifies a bounded full source recovery checkpoint.',
        'type' => 'write',
        'ajax' => false,
        'capabilities' => 'local/programming_bridge:storecheckpoint',
    ],
    'local_programming_bridge_get_latest_checkpoint' => [
        'classname' => 'local_programming_bridge\\external\\get_latest_checkpoint',
        'description' => 'Returns the latest integrity-verified recovery checkpoint for an attempt.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'local/programming_bridge:storecheckpoint',
    ],
    'local_programming_bridge_upsert_task_definition' => [
        'classname' => 'local_programming_bridge\\external\\upsert_task_definition',
        'description' => 'Idempotently mirrors one immutable programming-task version.',
        'type' => 'write',
        'ajax' => false,
        'capabilities' => 'local/programming_bridge:synctaskbank',
    ],
    'local_programming_bridge_get_task_bank_snapshot' => [
        'classname' => 'local_programming_bridge\\external\\get_task_bank_snapshot',
        'description' => 'Returns the connector-owned programming-task mirror for recovery.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'local/programming_bridge:synctaskbank',
    ],
];

$services = [
    'Programming platform bridge' => [
        'functions' => array_keys($functions),
        'restrictedusers' => 1,
        'enabled' => 0,
        'shortname' => 'programming_bridge',
        'downloadfiles' => 0,
        'uploadfiles' => 0,
    ],
];
