# Окружение симулятора внутри VM "sim" (source). Используется vm_start.sh и замерами.
# setup.bash не совместим с set -u.
case "$-" in *u*) _sim_u=1; set +u;; *) _sim_u=0;; esac
source /opt/ros/jazzy/setup.bash
[ "$_sim_u" = 1 ] && set -u; unset _sim_u
export TURTLEBOT3_MODEL="${TURTLEBOT3_MODEL:-waffle_pi}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-30}"
# Установлен только Cyclone DDS (--no-install-recommends не тянет Fast DDS) — фиксируем явно.
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
# Большой приёмный буфер DDS: иначе best-effort подписчики (rosbridge) теряют кадры камеры (см. cyclonedds.xml).
export CYCLONEDDS_URI="${CYCLONEDDS_URI:-file://$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cyclonedds.xml}"
# DDS только внутри VM: наружу граф ROS не нужен, связь агента — через rosbridge (один TCP-порт).
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
# Рендеринг сенсоров (камера, gpu_lidar) — программный: в VM нет GPU, Mesa llvmpipe через EGL.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export LIBGL_DRI3_DISABLE=1
export QT_QPA_PLATFORM=offscreen
export SIM_RUN_ROOT="${SIM_RUN_ROOT:-$HOME/robot_sim}"
