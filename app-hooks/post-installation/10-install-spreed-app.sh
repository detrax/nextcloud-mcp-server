#!/bin/bash

set -euox pipefail

# Talk (spreed) ships its appstore release on demand; on a fresh container the
# app is not yet installed, so app:install pulls it. On rebuilds where the
# volume already has it, app:install would fail, so fall through to
# app:enable.
php /var/www/html/occ app:install spreed || php /var/www/html/occ app:enable spreed
