package agent

// ExecutionTasks is a detached scheduling view of already validated metadata.
// It is not custody evidence or effect authority. The original public Plan must
// survive a partial resume or a single-role rerun outside that plan.
func (run RunMetadata) ExecutionTasks() []RunPlanNode {
	var source []RunPlanNode
	if run.Recovery == nil {
		source = run.Plan
	} else {
		if run.Recovery.Metadata == nil {
			return nil
		}
		for _, node := range run.Recovery.Metadata.Execute {
			if node == nil {
				return nil
			}
			source = append(source, RunPlanNode{ID: node.Id, Task: node.Task, DependsOn: node.DependsOn})
		}
	}
	result := make([]RunPlanNode, 0, len(source))
	for _, node := range source {
		copy := node
		copy.DependsOn = append([]string(nil), node.DependsOn...)
		result = append(result, copy)
	}
	return result
}
