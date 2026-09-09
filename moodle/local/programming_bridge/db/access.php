<?php

defined('MOODLE_INTERNAL') || die();

$capabilities = [
    'local/programming_bridge:use' => [
        'captype' => 'read',
        'contextlevel' => CONTEXT_COURSE,
        'archetypes' => [
            'student' => CAP_ALLOW,
            'teacher' => CAP_ALLOW,
            'editingteacher' => CAP_ALLOW,
            'manager' => CAP_ALLOW,
        ],
    ],
    'local/programming_bridge:manage' => [
        'captype' => 'write',
        'riskbitmask' => RISK_CONFIG,
        'contextlevel' => CONTEXT_SYSTEM,
        'archetypes' => ['manager' => CAP_ALLOW],
    ],
    'local/programming_bridge:pushgrade' => [
        'captype' => 'write',
        'riskbitmask' => RISK_PERSONAL,
        'contextlevel' => CONTEXT_COURSE,
        'archetypes' => [
            'editingteacher' => CAP_ALLOW,
            'manager' => CAP_ALLOW,
        ],
    ],
    'local/programming_bridge:storecheckpoint' => [
        'captype' => 'write',
        'riskbitmask' => RISK_PERSONAL,
        'contextlevel' => CONTEXT_COURSE,
        // This capability is intentionally not granted to students or teachers.
        // Assign it only to the restricted web-service account used by the platform.
        'archetypes' => [
            'manager' => CAP_ALLOW,
        ],
    ],
    'local/programming_bridge:synctaskbank' => [
        'captype' => 'write',
        'riskbitmask' => RISK_CONFIG,
        'contextlevel' => CONTEXT_COURSE,
        // Task definitions can include private tests. Grant only to the restricted service user.
        'archetypes' => [
            'manager' => CAP_ALLOW,
        ],
    ],
];
