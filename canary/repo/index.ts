// Synthetic canary simulating a breaking v2 SDK rename: formatBanner -> renderBanner
export function renderBanner(name: string): string {
  return `[v2] Hello, ${name}!`;
}

export function buildGreeting(user: string): string {
  // BUG introduced by major bump: old symbol formatBanner no longer exists
  return formatBanner(user);
}
