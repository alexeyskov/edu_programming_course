<?php

$string['pluginname'] = 'Programming platform bridge';
$string['platformurl'] = 'Programming platform URL';
$string['platformurl_desc'] = 'Exact HTTPS origin of the programming platform, without a trailing slash.';
$string['sharedsecret'] = 'Bridge shared secret';
$string['sharedsecret_desc'] = 'A random 32-byte or longer secret used to sign short-lived launch assertions.';
$string['privacy:metadata:local_prgbridge_checkpoint'] = 'Stores references to source-code checkpoints sent by the programming platform.';
$string['privacy:metadata:local_prgbridge_checkpoint:userid'] = 'The Moodle user associated with the checkpoint.';
$string['privacy:metadata:local_prgbridge_checkpoint:attemptref'] = 'The opaque attempt identifier in the programming platform.';
$string['privacy:metadata:local_prgbridge_checkpoint:snapshotref'] = 'The opaque snapshot identifier in the programming platform.';
$string['privacy:metadata:local_prgbridge_checkpoint:snapshotsha256'] = 'Integrity hash of the canonical source manifest.';
$string['privacy:metadata:local_prgbridge_checkpoint:eventchainhead'] = 'Integrity anchor of the edit-event chain.';
$string['privacy:metadata:local_prgbridge_checkpoint:epoch'] = 'Attempt edit-history epoch.';
$string['privacy:metadata:local_prgbridge_checkpoint:workspacerevision'] = 'Acknowledged workspace revision.';
$string['privacy:metadata:local_prgbridge_checkpoint:manifestjson'] = 'The full canonical source manifest used as a recovery copy.';
$string['privacy:metadata:local_prgbridge_checkpoint:reason'] = 'Why the recovery checkpoint was created.';
$string['privacy:metadata:local_prgbridge_checkpoint:timecreated'] = 'When the checkpoint reference was received.';
$string['programming_bridge:use'] = 'Use the programming platform bridge';
$string['programming_bridge:manage'] = 'Manage the programming platform bridge';
$string['programming_bridge:pushgrade'] = 'Push grades through the programming platform bridge';
$string['programming_bridge:storecheckpoint'] = 'Store recovery checkpoints through the programming platform bridge';
$string['programming_bridge:synctaskbank'] = 'Synchronize the programming task-bank mirror';
$string['missingconfiguration'] = 'The programming platform bridge is not configured.';
$string['invalidreturnurl'] = 'The configured platform URL is invalid.';
$string['openplatform'] = 'Open programming workspace';
$string['launchconfirm'] = 'Continue to the configured programming platform as the currently signed-in Moodle user.';
$string['storedcheckpointcorrupt'] = 'The stored recovery checkpoint failed integrity verification.';
