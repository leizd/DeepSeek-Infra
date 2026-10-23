//go:build linux && (android || deepseek_android)

package launch

func platformBinaryCandidates(name string) []string {
	packaged := map[string]string{
		"deepseekd":        "libdeepseek_control.so",
		"deepseek-worker":  "libdeepseek_worker.so",
		"deepseek-gateway": "libdeepseek_gateway.so",
	}
	if path, ok := packaged[name]; ok {
		return []string{path, name}
	}
	return []string{name}
}
