<?php
// SECURITY CANARY — do not remove or modify.
//
// If phpcs (or any PHP process) ever loads this file because the phpcs
// ruleset in this directory named it (bootstrap, autoload, installed_paths),
// the invariant "never execute checked-out code" has been broken. A sentinel
// file is written to CANARY_DIR to make this detectable in tests.
$dir = getenv('CANARY_DIR') ?: '/tmp/canary';
@mkdir($dir, 0777, true);
@touch($dir . '/phpcs-ruleset-executed');
