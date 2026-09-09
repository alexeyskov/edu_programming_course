<?php

defined('MOODLE_INTERNAL') || die();

if ($hassiteconfig) {
    $settings = new admin_settingpage(
        'local_programming_bridge',
        get_string('pluginname', 'local_programming_bridge')
    );
    $ADMIN->add('localplugins', $settings);

    $settings->add(new admin_setting_configtext(
        'local_programming_bridge/platformurl',
        get_string('platformurl', 'local_programming_bridge'),
        get_string('platformurl_desc', 'local_programming_bridge'),
        '',
        PARAM_URL
    ));
    $settings->add(new admin_setting_configpasswordunmask(
        'local_programming_bridge/sharedsecret',
        get_string('sharedsecret', 'local_programming_bridge'),
        get_string('sharedsecret_desc', 'local_programming_bridge'),
        ''
    ));
}

