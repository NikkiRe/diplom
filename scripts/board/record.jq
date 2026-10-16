# Итог юнита board-run-<имя> (board.sh run): jq -n [--rawfile <файл> <путь> …] [--arg unit|wall_s|signum …] -f record.jq
# Сырые файлы снимает ExecStopPost юнита, пока процесс ещё в cgroup: файлы cgroup v2 (memory.peak, memory.events,
# cpu.stat, io.stat …) и exit — три строки: SERVICE_RESULT, EXIT_CODE, EXIT_STATUS (systemd.exec(5)).
# Отсутствующий файл — null. OOM-kill любого процесса юнита даёт ненулевой код: 137, если главный процесс вышел с 0.
# Истёк срок (RuntimeMaxSec, SERVICE_RESULT=timeout) — reason timeout, код по сигналу остановки (обычно 143);
# если команда сама вышла по нему с 0 — 124, как у timeout(1).
$ARGS.named as $a
| def raw($name): $a[$name] | if . == null then null else rtrimstr("\n") end;
  def num($name): raw($name) | if . == null then null else (tonumber? // .) end;    # «max» остаётся строкой
  def pairs($name): raw($name) | if . == null then null
                    else [scan("(\\S+) (\\d+)") | {(.[0]): (.[1] | tonumber)}] | add // {} end;
  def devices($name): raw($name) | if . == null then null
                      else [splits("\n") | select(length > 0) | split(" ") | select(length > 0)
                            | {(.[0]): ([.[1:][] | select(length > 0) | split("=")
                                         | {(.[0]): (.[1] | tonumber? // .)}] | add // {})}] | add // {} end;
  (raw("exit") // "" | split("\n")) as $exit
| ($exit[1] // "") as $how
| ($exit[2] // "") as $status
| (pairs("memory.events") // {}) as $events
| (if $how == "exited" then ($status | tonumber)
   elif $how == "killed" or $how == "dumped" then 128 + ($a.signum | tonumber)
   else 125 end) as $code
| (if ($events.oom_kill // 0) > 0 then "oom-kill"
   elif $exit[0] == "timeout" then "timeout"
   elif $how == "exited" then "exit"
   elif $how == "killed" or $how == "dumped" then "signal"
   else "not-started" end) as $reason
| {unit: $a.unit,
   reason: $reason,
   exit_code: (if $code != 0 then $code elif $reason == "oom-kill" then 137 elif $reason == "timeout" then 124
               else 0 end),
   signal: (if $how == "killed" or $how == "dumped" then "SIG\($status)" else null end),
   service_result: ($exit[0] // null),
   wall_s: ($a.wall_s | tonumber? // null),
   memory: {max: num("memory.max"), swap_max: num("memory.swap.max"), peak: num("memory.peak"), events: $events},
   cpu: {max: raw("cpu.max"), cpus: raw("cpuset.cpus.effective"), stat: pairs("cpu.stat")},
   io: {max: devices("io.max"), stat: devices("io.stat")},
   pids_peak: num("pids.peak")}
