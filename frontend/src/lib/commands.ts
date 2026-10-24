import type { CommandOut, TaskDetail } from './types'

/** Stage of commands the user ran themselves (the agent's own runs use test stages). */
export const USER_STAGE = 'user'

export function isUserCommand(command: CommandOut): boolean {
  return command.stage === USER_STAGE
}

export function commandLine(command: CommandOut): string {
  return command.argv.join(' ')
}

/** Whether a command's test summary means something (a user's `python main.py` has no test framework). */
export function hasTestSummary(command: CommandOut): boolean {
  return !!command.summary && !(isUserCommand(command) && command.summary.framework === 'unknown')
}

/** A run of the user's that ended badly (not one they stopped themselves). */
export function isFailedUserRun(command: CommandOut): boolean {
  if (!isUserCommand(command)) return false
  return command.status === 'timed_out' || command.status === 'error' ||
    (command.status === 'completed' && command.exit_code !== 0)
}

export function runningUserCommand(task: TaskDetail | null | undefined): CommandOut | null {
  return task?.commands.find((c) => isUserCommand(c) && c.status === 'running') ?? null
}

/** Distinct command lines the user ran in this task, newest first (for ↑/↓ recall). */
export function userCommandHistory(task: TaskDetail | null | undefined): string[] {
  const lines = (task?.commands ?? []).filter(isUserCommand).map(commandLine).reverse()
  return [...new Set(lines)]
}

/**
 * The user's runs since the last turn, which the backend shares with LocPilot on the next message
 * (mirrors `TaskManager._recent_user_runs`).
 */
export function sharedUserRuns(task: TaskDetail | null | undefined, limit = 2): CommandOut[] {
  if (!task) return []
  return task.commands
    .filter((c) => isUserCommand(c) && c.turn === task.turn && c.status !== 'running')
    .slice(-limit)
}
