# Профиль neurobot для start_sim.sh (source): стенд робота NeuroChip — neurobot/sim/stand/README.md.
# Задаёт умолчания start_sim.sh и launch-файл стенда; явные флаги start_sim.sh их переопределяют.
STAND="$PROJ/neurobot/sim/stand"
LAUNCH="$STAND/launch/neurobot_stand.launch.py"
WORLD="$STAND/worlds/lab_wall.sdf"
# Камера Gazebo удалена (RTF, docs/robot_sim.md §7 «F»); /odom 50 Гц с исправленным именем параметра DiffDrive;
# сервисы мира — для сброса позы робота и сценарных препятствий (world_events.py).
CAMMODE=off ODOM=50 WCTL=true
# Лидар — как в скане с робота (urg_node2, 02.06.2026; docs/neurobot/research/R7_robot_slides.md, RS15): 683 луча
# симметрично ±2,0923497677 рад (шаг 2π/1024), до 5,6 м. Ближнюю границу задаёт база (stand_config.py: TB3 — 0,06 м,
# корпус LDS; neurochip — 0,02 м, как на роботе).
PROFILE_ARGS=(odom_hz_fix:=true ground_truth_hz:=100 lidar_samples:=683 lidar_min_angle:=-2.0923497677
  lidar_max_angle:=2.0923497677 lidar_range_max:=5.6)
READY_CHECK="python3 $STAND/stand_ready.py --timeout 30"
