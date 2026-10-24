const GIT_URL_RE = /^(https:\/\/(github\.com|gitlab\.com|bitbucket\.org)\/[\w.-]+\/[\w.-]+?(\.git)?\/?|git@(github\.com|gitlab\.com|bitbucket\.org):[\w.-]+\/[\w.-]+?(\.git)?)$/

/** Client-side hint only; the backend performs the authoritative validation. */
export function validateRepoUrl(url: string): string | null {
  const u = url.trim()
  if (!u) return 'Enter a repository URL.'
  if (/^https?:\/\/[^/]*@/.test(u)) return 'Remove credentials from the URL. Use SSH keys or a Git credential helper for private repositories.'
  if (u.startsWith('http://')) return 'Use an https:// URL.'
  if (!GIT_URL_RE.test(u)) return 'Expected https://github.com/<owner>/<repo> or git@github.com:<owner>/<repo>.git'
  return null
}
