"""Окно наблюдателя здоровья: счёт сообщений, состояния, ненулевые /cmd_vel, сброс окна."""
from neurobot.sim.budget.health import TOPICS, Window


def test_window_counts_and_states():
    w = Window()
    w.add_json("health", '{"fps": 9.5, "state": "ok"}')
    for st in ("ok", "ok", "slow"):
        w.add_json("safety", '{"state": "%s", "reasons": []}' % st)
    w.add_json("truth", '{"rtf": 0.97}')
    w.add_json("gesture", '{"name": "turn180"}')
    w.add_twist("cmd_vel", 0.0, 0.0)
    w.add_twist("cmd_vel", 0.1, 0.0)
    w.add_twist("cmd_req", 0.0, 0.5)
    rec = w.flush(100.0, 1.0)
    assert rec["n"] == {"health": 1, "safety": 3, "truth": 1, "gesture": 1, "cmd_vel": 2, "cmd_req": 1}
    assert rec["safety"] == {"ok": 2, "slow": 1} and rec["cmd_vel_nonzero"] == 1
    assert rec["fps"] == 9.5 and rec["perception"] == "ok" and rec["rtf"] == 0.97 and rec["gesture"] == "turn180"


def test_window_reset_forgets_values_of_silent_publishers():
    w = Window()
    w.add_json("health", '{"fps": 10, "state": "paused"}')
    w.add_json("gesture", '{"name": "come"}')
    w.add_json("truth", '{"rtf": 0.9}')
    assert w.flush(1.0, 1.0)["fps"] == 10
    rec = w.flush(2.0, 1.0)                     # perception замолчал (например, убит OOM)
    assert rec == {"t_wall": 2.0, "dt": 1.0, "n": {}, "safety": {}, "cmd_vel_nonzero": 0}


def test_bad_json_counted_not_raised():
    w = Window()
    w.add_json("safety", "{не json")
    w.add_json("health", "[1, 2]")
    assert w.flush(0.0, 1.0)["n"] == {"safety": 1, "safety_bad": 1, "health": 1, "health_bad": 1}


def test_topics_are_robot_names():
    assert TOPICS["cmd_vel"] == "/cmd_vel" and TOPICS["mux_out"] == "/diff_cont/cmd_vel_unstamped"
    assert TOPICS["safety"] == "/neurobot/safety_state"
