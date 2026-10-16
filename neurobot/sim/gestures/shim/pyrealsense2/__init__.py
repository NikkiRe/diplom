"""Заглушка pyrealsense2 для стенда neurobot — реэкспорт neurobot.sim.gestures.realsense (описание там).

В PYTHONPATH нужны оба каталога: <корень проекта> и <корень>/neurobot/sim/gestures/shim.
"""
try:
    from neurobot.sim.gestures.realsense import (camera_info, composite_frame, config, configure, context,  # noqa: F401
                                                 device, format, option, pipeline, pipeline_profile, sensor,
                                                 stream, video_frame)
except ImportError as e:
    raise ImportError("заглушка pyrealsense2: нет пакета neurobot (корень проекта не в PYTHONPATH): %s" % e) from e
