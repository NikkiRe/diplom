# Аргументы tc netem одного направления сетевого профиля: jq -r --arg p <профиль> --arg d <up|down> -f netem.jq
# bench/board/profiles.json. Параметры, которых нет в профиле, задаются явно: tc qdisc replace сохраняет
# прежнюю скорость, если rate не передан, поэтому «без ограничения» — rate 0kbit.
# limit (пакеты в netem вместе с задержанными) — объём rate·(delay + jitter + queue.ms) пакетами по 1514 байт
# в пределах [queue.min_packets, queue.max_packets]; без rate — 1000, умолчание netem. Нет статического профиля —
# ошибка jq: пустые аргументы дали бы netem без ограничений.
.net.queue as $q
| (.net.profiles[$p][$d] // error("нет статического профиля \($p | tojson)")) as $x
| ($x.rate_kbit // 0) as $r
| ["delay", "\($x.delay_ms // 0)ms"]
  + (if ($x.jitter_ms // 0) > 0 then ["\($x.jitter_ms)ms"] else [] end)
  + ["rate", "\($r)kbit"]
  + (if ($x.loss_pct // 0) > 0 then ["loss", "random", "\($x.loss_pct)%"]
     elif $x.gemodel then
       ["loss", "gemodel"] + ($x.gemodel | [.p_pct, .r_pct, .bad_loss_pct, .good_loss_pct] | map("\(.)%"))
     else [] end)
  + ["limit", (if $r > 0
               then [$q.min_packets,
                     ($r * 125 * (($x.delay_ms // 0) + ($x.jitter_ms // 0) + $q.ms) / 1000 / 1514 | ceil),
                     $q.max_packets] | sort | .[1]
               else 1000 end | tostring)]
| join(" ")
