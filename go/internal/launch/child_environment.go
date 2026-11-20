package launch

import "strings"

// Apply shared overrides to the actual child environments while retaining their
// role restrictions and process-specific settings. Copy both slices so an
// earlier plan can still be started independently.
func withProductionEnvironment(plan Plan, overrides []string) Plan {
	keys := make(map[string]struct{}, len(overrides))
	for _, entry := range overrides {
		key, _, _ := strings.Cut(entry, "=")
		keys[key] = struct{}{}
	}
	merge := func(current []string) []string {
		result := make([]string, 0, len(current)+len(overrides))
		for _, entry := range current {
			key, _, _ := strings.Cut(entry, "=")
			if _, replaced := keys[key]; !replaced {
				result = append(result, entry)
			}
		}
		return append(result, overrides...)
	}
	updated := plan
	updated.Env = merge(plan.Env)
	updated.Processes = append([]Process(nil), plan.Processes...)
	for index, process := range plan.Processes {
		// WithDesktop already built a reduced UI environment. Backend changes
		// must never replace it or make a nil UI inherit the parent's secrets.
		if process.Name == "deepseek-desktop" {
			updated.Processes[index].Env = append([]string{}, process.Env...)
			continue
		}
		current := process.Env
		if current == nil {
			current = plan.Env
		}
		updated.Processes[index].Env = productionChildEnvironment(process.Name, merge(current))
	}
	return updated
}
