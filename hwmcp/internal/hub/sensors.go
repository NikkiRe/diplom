package hub

import (
	"math"

	"github.com/local/hwmcp/internal/sysfs"
)

// Живые датчики. Устройство пропадает и появляется (горячее отключение, перезагрузка драйвера), атрибут
// временно не читается (EIO у DHT11, NACK шины I²C), значение «застревает». Показание тогда становится
// null с причиной (sysfs.Reason*, ReasonStuck, ошибка линии GPIO), подписчики получают уведомление, а
// сбои считаются по свойствам: board_health (что недоступно сейчас), hw://diag и run_end (история).

// ReasonStuck — число не менялось дольше окна stuck_after_s: показанию нельзя верить.
const ReasonStuck = "stuck"

// faultLogEvery — записи журнала о сбоях одного свойства не чаще раза за столько секунд по часам
// hw-mcp: «мигающий» датчик не засоряет журнал на SD-карте, а счётчики остаются полными.
const faultLogEvery = 60.0

// faults — сбои свойства (под stMu).
type faults struct {
	reads      int     // неудачных чтений всего
	outages    int     // переходов в «недоступно» (null)
	downSince  float64 // начало текущего перерыва
	logged     bool    // текущий перерыв записан в журнал (тогда записывается и его конец)
	quietUntil float64 // до этого момента записи о сбоях свойства не пишутся
	rebound    bool    // в текущем перерыве устройство уже найдено повторной инвентаризацией
}

// stuckLocked — число равно предыдущему хорошему отсчёту дольше окна stuck_after_s.
func (ks *keyState) stuckLocked(r Reading, now float64) bool {
	f, ok := r.Value.(float64)
	return ks.stuck > 0 && ok && ks.hasPrev && f == ks.prev && now-ks.since >= ks.stuck
}

// unknownLocked — значение свойства становится неизвестным (null) с причиной r.Err; true — сообщить.
func (h *Hub) unknownLocked(ks *keyState, r Reading, now float64) bool {
	h.faultLocked(ks, r.Err, now)
	ks.r, ks.has = r, true
	return h.reportLocked(ks, nil, now)
}

// faultLocked — значение свойства неизвестно (null): переход в «недоступно» считается, а в журнал пишутся
// начало перерыва и смена причины, не чаще faultLogEvery на свойство. Перерыв, начавшийся внутри окна
// ограничения, записывается по окончании окна, если ещё длится: затяжной сбой после «мигания» не теряется.
func (h *Hub) faultLocked(ks *keyState, reason string, now float64) {
	f := &ks.faults
	changed := !ks.has || ks.r.Err != reason
	if !ks.has || ks.r.Err == "" {
		f.outages++
		f.downSince, f.logged, f.rebound = now, false, false
		h.outages.Add(1)
	}
	switch {
	case f.logged && !changed:
		return
	case now < f.quietUntil:
		if changed {
			h.quietFaults.Add(1)
		}
		return
	}
	f.logged, f.quietUntil = true, now+faultLogEvery
	h.opts.Trace.Emit("sample_error", map[string]any{"device": ks.dev.ID, "property": ks.prop.Name,
		"error": reason, "consecutive": ks.fails, "outages": f.outages, "down_s": round3(now - f.downSince),
		"ts_clock": now})
	h.log.Warn("sensor value unavailable", "key", ks.key, "reason", reason)
}

// recoveredLocked — значение снова есть; в журнал — парой к записанному началу перерыва.
func (h *Hub) recoveredLocked(ks *keyState, now float64) {
	h.recoveries.Add(1)
	f := &ks.faults
	if !f.logged {
		return
	}
	f.logged = false
	down := round3(now - f.downSince)
	h.opts.Trace.Emit("sample_recovered", map[string]any{"device": ks.dev.ID, "property": ks.prop.Name,
		"down_s": down, "ts_clock": now})
	h.log.Info("sensor value recovered", "key", ks.key, "down_s", down)
}

// boxed упаковывает число в any, переиспользуя прошлую упаковку того же значения: неизменное показание
// не выделяет память на каждом опросе (atomic.Value: читают и сэмплер, и read_property).
func (ks *keyState) boxed(v float64) any {
	if b := ks.box.Load(); b != nil && b.(float64) == v {
		return b
	}
	var b any = v
	ks.box.Store(b)
	return b
}

// rescanMaxPeriod — предел роста периода повторной инвентаризации, с (допущение): снятое насовсем
// устройство не ищется каждые RescanPeriod всё время работы, а вернувшееся находится не позже чем через
// столько.
const rescanMaxPeriod = 300.0

// rescanDueLocked планирует повторную инвентаризацию, пока есть пропавшее устройство (lost); true — пора.
// Период удваивается после каждой инвентаризации до rescanMaxPeriod и сбрасывается, когда пропавших нет.
func (h *Hub) rescanDueLocked(lost bool, now float64) bool {
	switch {
	case !lost || h.opts.RescanPeriod <= 0:
		h.rescanAt, h.rescanEvery = math.Inf(1), h.opts.RescanPeriod
	case math.IsInf(h.rescanAt, 1):
		h.rescanAt = now + h.rescanEvery
	case now >= h.rescanAt-dueSlack:
		h.rescanEvery = min(2*h.rescanEvery, max(rescanMaxPeriod, h.opts.RescanPeriod))
		h.rescanAt = now + h.rescanEvery
		return true
	}
	return false
}

// rescan ищет пропавшие устройства IIO и hwmon заново — по id инвентаря, родителю на шине и имени канала
// (relocKey) — и привязывает к ним читателей (sysfs.Reader.Retarget): по новому пути или по тому же,
// если читатель удерживал дескрипторы пропавшего узла. Значение вернётся со следующим опросом.
// Устройства, которых не было при старте, в модель не добавляются: инвентарь — стабильный префикс
// промпта агента.
func (h *Hub) rescan(now float64) {
	h.rescans.Add(1)
	found := map[string]*sysfs.Channel{}
	for _, d := range sysfs.ProbeRenumbered(h.opts.Root) {
		for i := range d.Channels {
			k := relocKey(d, d.Channels[i].Name)
			if _, dup := found[k]; dup {
				found[k] = nil // два устройства с одним id и родителем: не угадывать
				continue
			}
			found[k] = &d.Channels[i]
		}
	}
	for _, ks := range h.inputs {
		if !ks.relocate {
			continue
		}
		h.stMu.RLock()
		lost := ks.r.Err == sysfs.ReasonAbsent
		h.stMu.RUnlock()
		c := found[relocKey(ks.dev.Raw, ks.prop.Title)]
		if !lost || c == nil || !ks.reader.Retarget(c) {
			continue
		}
		h.rebound.Add(1)
		h.stMu.Lock()
		first := !ks.faults.rebound
		ks.faults.rebound = true
		h.stMu.Unlock()
		// повторные привязки в том же перерыве (драйвер присутствующего устройства отвечает ENODEV) — только
		// в счётчике
		if first {
			h.opts.Trace.Emit("sensor_rebound", map[string]any{"device": ks.dev.ID, "property": ks.prop.Name,
				"path": c.Path, "ts_clock": now})
			h.log.Info("sensor found again", "key", ks.key, "path", c.Path)
		}
	}
}

// relocKey — устройство и канал при повторной инвентаризации. У hwmon id инвентаря — только имя драйвера,
// и одинаковые чипы (два lm75 на шине I²C) различает лишь родитель (1-0048 и 1-0049).
func relocKey(d *sysfs.Device, channel string) string {
	return d.ID + "\x00" + *d.Bus + "\x00" + channel
}

// SensorFaults — свойства, значение которых сейчас неизвестно, и причина: "<устройство>.<свойство>" ->
// причина (для board_health).
func (h *Hub) SensorFaults() map[string]any {
	out := map[string]any{}
	h.stMu.RLock()
	defer h.stMu.RUnlock()
	for key, ks := range h.keys {
		if ks.has && ks.r.Err != "" {
			out[key] = ks.r.Err
		}
	}
	return out
}

// sensorHistory — сбои по свойствам с начала работы (hw://diag); только свойства, у которых они были.
func (h *Hub) sensorHistory() map[string]any {
	now := h.Now()
	out := map[string]any{}
	h.stMu.RLock()
	defer h.stMu.RUnlock()
	for key, ks := range h.keys {
		f := ks.faults
		if f.reads == 0 && f.outages == 0 {
			continue
		}
		v := map[string]any{"read_errors": f.reads, "outages": f.outages, "error": nilIfEmpty(ks.r.Err)}
		if ks.r.Err != "" {
			v["down_s"] = round3(now - f.downSince)
		}
		out[key] = v
	}
	return out
}
