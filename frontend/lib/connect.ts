/** Connection details for AI clients, built from the site's own address and a fresh token. */
export interface Snippets {
  link: string;
  endpoint: string;
  claudeCode: string;
  gemini: string;
}

export function snippets(origin: string, token: string): Snippets {
  const endpoint = `${origin.replace(/\/$/, "")}/mcp`;
  return {
    endpoint,
    link: `${endpoint}/t/${token}`,
    claudeCode: `claude mcp add --transport http singularity ${endpoint} --header "Authorization: Bearer ${token}"`,
    gemini: JSON.stringify({ mcpServers: { singularity: { httpUrl: endpoint, headers: { Authorization: `Bearer ${token}` } } } }, null, 2),
  };
}
