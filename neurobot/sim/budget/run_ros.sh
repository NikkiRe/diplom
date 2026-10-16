#!/usr/bin/env bash
# (ВМ sim) ExecStart юнитов бюджета: окружение ROS 2 стенда (scripts/sim/vm_env.sh) и exec команды без смены PID.
# Домен DDS — ROS_DOMAIN_ID из окружения юнита: vm_env.sh ставит 30, только если переменная пуста.
set -eo pipefail
# shellcheck source=../../../scripts/sim/vm_env.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../../scripts/sim/vm_env.sh"
exec "$@"
