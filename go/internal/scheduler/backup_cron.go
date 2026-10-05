package scheduler

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"time"
	_ "time/tzdata" // Keep IANA schedule semantics in a clean native release package.
)

// BackupNextRun is the existing /api/workspace/backup-policies nextRuns value.
type BackupNextRun struct {
	ScheduledFor  string `json:"scheduledFor"`
	LocalDateTime string `json:"localDateTime"`
	Timezone      string `json:"timezone"`
	SlotKey       string `json:"slotKey"`
	JitterSeconds int    `json:"jitterSeconds"`
}

type backupCron struct {
	minutes, hours, days, months, weekdays []int
	domRestricted, dowRestricted           bool
}

func backupCronField(raw string, min, max int, weekday bool) ([]int, bool) {
	selected := make(map[int]bool)
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return nil, false
		}
		step := 1
		if pieces := strings.Split(part, "/"); len(pieces) == 2 {
			part = pieces[0]
			var err error
			step, err = strconv.Atoi(strings.TrimSpace(pieces[1]))
			if err != nil || step <= 0 {
				return nil, false
			}
		} else if len(pieces) != 1 {
			return nil, false
		}
		first, last := min, max
		if part != "*" {
			bounds := strings.Split(part, "-")
			if len(bounds) > 2 || len(bounds) == 0 {
				return nil, false
			}
			var err error
			first, err = strconv.Atoi(strings.TrimSpace(bounds[0]))
			if err != nil {
				return nil, false
			}
			last = first
			if len(bounds) == 2 {
				last, err = strconv.Atoi(strings.TrimSpace(bounds[1]))
				if err != nil {
					return nil, false
				}
			}
		}
		if first < min || last > max || first > last {
			return nil, false
		}
		for value := first; value <= last; value += step {
			if weekday && value == 7 {
				selected[0] = true
				continue
			}
			selected[value] = true
		}
	}
	if len(selected) == 0 {
		return nil, false
	}
	values := make([]int, 0, len(selected))
	for value := range selected {
		values = append(values, value)
	}
	sort.Ints(values)
	return values, true
}

func parseBackupCron(raw string) (backupCron, bool) {
	fields := strings.Fields(raw)
	if len(fields) != 5 {
		return backupCron{}, false
	}
	var parsed backupCron
	var ok bool
	if parsed.minutes, ok = backupCronField(fields[0], 0, 59, false); !ok {
		return backupCron{}, false
	}
	if parsed.hours, ok = backupCronField(fields[1], 0, 23, false); !ok {
		return backupCron{}, false
	}
	if parsed.days, ok = backupCronField(fields[2], 1, 31, false); !ok {
		return backupCron{}, false
	}
	if parsed.months, ok = backupCronField(fields[3], 1, 12, false); !ok {
		return backupCron{}, false
	}
	if parsed.weekdays, ok = backupCronField(fields[4], 0, 7, true); !ok {
		return backupCron{}, false
	}
	parsed.domRestricted = fields[2] != "*"
	parsed.dowRestricted = fields[4] != "*"
	return parsed, true
}

func hasInt(values []int, wanted int) bool {
	index := sort.SearchInts(values, wanted)
	return index < len(values) && values[index] == wanted
}

func (cron backupCron) matches(day time.Time) bool {
	if !hasInt(cron.months, int(day.Month())) {
		return false
	}
	dom := hasInt(cron.days, day.Day())
	dow := hasInt(cron.weekdays, int(day.Weekday()))
	if cron.domRestricted && cron.dowRestricted {
		return dom || dow
	}
	return dom && dow
}

// resolveBackupLocal selects the first UTC occurrence of an ambiguous local
// minute, and rejects a nonexistent minute. Sampling nearby UTC offsets avoids
// time.Date's platform-dependent normalization across DST gaps and folds.
func resolveBackupLocal(local time.Time, zone *time.Location) (time.Time, bool) {
	offsets := make(map[int]bool)
	for h := -48; h <= 48; h += 6 {
		_, offset := local.Add(time.Duration(h) * time.Hour).In(zone).Zone()
		offsets[offset] = true
	}
	var earliest time.Time
	for offset := range offsets {
		candidate := local.Add(-time.Duration(offset) * time.Second)
		wall := candidate.In(zone)
		if wall.Year() == local.Year() && wall.Month() == local.Month() && wall.Day() == local.Day() &&
			wall.Hour() == local.Hour() && wall.Minute() == local.Minute() &&
			(earliest.IsZero() || candidate.Before(earliest)) {
			earliest = candidate
		}
	}
	return earliest, !earliest.IsZero()
}

func backupString(value any, fallback string) string {
	text, ok := value.(string)
	if !ok || text == "" {
		return fallback
	}
	return text
}

// NextBackupRun ports the Python backup_cron.next_slot and next_run_for_policy
// public projection. It does not claim or execute a slot.
func NextBackupRun(policy map[string]any, now time.Time) *BackupNextRun {
	config, ok := policy["schedule"].(map[string]any)
	if !ok {
		return nil
	}
	cron, ok := parseBackupCron(backupString(config["cron"], ""))
	if !ok {
		return nil
	}
	zoneName := backupString(config["timezone"], "UTC")
	zone, err := time.LoadLocation(zoneName)
	if err != nil {
		return nil
	}
	jitterMax := 0
	switch value := config["jitterSeconds"].(type) {
	case float64:
		if value < 0 || value > 3600 || value != float64(int(value)) {
			return nil
		}
		jitterMax = int(value)
	case int:
		jitterMax = value
	case json.Number:
		parsed, err := strconv.Atoi(string(value))
		if err != nil {
			return nil
		}
		jitterMax = parsed
	case nil:
	default:
		return nil
	}
	if jitterMax < 0 || jitterMax > 3600 {
		return nil
	}
	end := now.UTC().Add(400 * 24 * time.Hour)
	startWall, endWall := now.In(zone), end.In(zone)
	day := time.Date(startWall.Year(), startWall.Month(), startWall.Day(), 0, 0, 0, 0, time.UTC).AddDate(0, 0, -1)
	last := time.Date(endWall.Year(), endWall.Month(), endWall.Day(), 0, 0, 0, 0, time.UTC).AddDate(0, 0, 1)
	for ; !day.After(last); day = day.AddDate(0, 0, 1) {
		if !cron.matches(day) {
			continue
		}
		for _, hour := range cron.hours {
			for _, minute := range cron.minutes {
				local := time.Date(day.Year(), day.Month(), day.Day(), hour, minute, 0, 0, time.UTC)
				instant, valid := resolveBackupLocal(local, zone)
				if !valid && backupString(config["misfirePolicy"], "skip") == "run-once" {
					for gap := 1; gap <= 180 && !valid; gap++ {
						instant, valid = resolveBackupLocal(local.Add(time.Duration(gap)*time.Minute), zone)
					}
				}
				if !valid || instant.Before(now) || !instant.Before(end) {
					continue
				}
				localISO := local.Format("2006-01-02T15:04")
				slotKey := localISO + "@" + zoneName
				jitter := 0
				if jitterMax > 0 {
					digest := sha256.Sum256([]byte(backupString(policy["policyId"], "") + "|" + slotKey))
					jitter = int(binary.BigEndian.Uint32(digest[:4]) % uint32(jitterMax+1))
				}
				return &BackupNextRun{
					ScheduledFor:  instant.Add(time.Duration(jitter) * time.Second).UTC().Format("2006-01-02T15:04:05Z"),
					LocalDateTime: localISO,
					Timezone:      zoneName,
					SlotKey:       slotKey,
					JitterSeconds: jitter,
				}
			}
		}
	}
	return nil
}
