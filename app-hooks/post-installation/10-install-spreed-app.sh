#!/bin/bash

set -euox pipefail

# Talk (spreed) ships its appstore release on demand; on a fresh container the
# app is not yet installed, so app:install pulls it. On rebuilds where the
# volume already has it, app:install would fail, so fall through to
# app:enable.
#
# Caveat: this `||` also masks unrelated install failures (network outage,
# bad version pin, etc.) — they will fall through to app:enable, which
# will then fail with a clearer "app not found" error. Acceptable for a
# dev-fixture script; do not copy this pattern into production tooling.
php /var/www/html/occ app:install spreed || php /var/www/html/occ app:enable spreed
