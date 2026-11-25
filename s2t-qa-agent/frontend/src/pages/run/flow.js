/** The straight flow of a run: four numbered steps, each done when its condition holds. */
export const FLOW = [
  { key: 'requirements', n: 1, label: 'Requirements', title: 'Complete the requirements' },
  { key: 'studio', n: 2, label: 'SQL', title: 'Review and approve the SQL' },
  { key: 'tests', n: 3, label: 'Test cases', title: 'Choose, approve and run the test cases' },
  { key: 'results', n: 4, label: 'Results & report', title: 'Results and report' },
]

export function stepState(run) {
  const sqlApproved = run.approvals?.TRANSFORMATION?.status === 'APPROVED'
  return {
    requirements: { done: !!run.requirements_ready && !!run.transformation_version,
      todo: run.counts.open_items ? `${run.counts.open_items} item(s) still need your input` : 'Waiting for the SQL to be generated' },
    studio: { done: sqlApproved, todo: 'Approve the SQL to continue' },
    tests: { done: run.counts.executions > 0, todo: 'Run the selected test cases to continue' },
    results: { done: run.counts.reports > 0, todo: '' },
  }
}

