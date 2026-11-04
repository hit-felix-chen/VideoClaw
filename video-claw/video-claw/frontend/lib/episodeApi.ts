import { confirmRequest, retryRequest, type NodeView, type QuoteView } from '@/lib/episodeFlow';

export interface ProjectRecord {
  id: string;
  name: string;
  budget_cap?: number | null;
  spent?: number | null;
  status?: string;
}

export interface GraphEdge {
  predecessor: string;
  successor: string;
  kind?: string;
  blocks?: boolean;
}

export interface GraphNode extends NodeView {
  episode_id?: string | null;
  metadata?: Record<string, unknown>;
  quote?: QuoteView | null;
  duration?: Record<string, unknown> | null;
  entry_id?: string | null;
  budget_hold?: boolean;
}

export interface ProjectStatus {
  project_id: string;
  budget?: {
    used?: number;
    committed?: number;
    remaining?: number | null;
    cap?: number | null;
    currency?: string | null;
  };
  ledger?: Array<Record<string, unknown>>;
  nodes: GraphNode[];
  edges: GraphEdge[];
  awaiting_user?: string[];
  executable?: string[];
  stale?: string[];
}

export interface EpisodeCreateResult {
  project: ProjectRecord;
  episode_id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface NodeActionResult {
  project_id?: string;
  node_id?: string | null;
  status?: string;
  gate?: string;
  message?: string;
  quote?: QuoteView | null;
  entry_id?: string | null;
  submitted?: boolean;
  spent?: boolean;
  provider_called?: boolean;
  call_confirmed?: boolean;
  over_budget?: boolean;
  result_id?: string;
  result?: Record<string, unknown>;
}

export interface ModelUpdateResult {
  updated: Array<{ node_id: string; model_id: string }>;
  blocked: Array<{ node_id: string; reason: string }>;
  unapplied: string[];
}

export interface StopResult {
  stopped: string[];
  refused: Array<{ node_id: string; reason: string }>;
  message: string;
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return '请求失败';
}

export async function readError(response: Response): Promise<string> {
  const data = await response.json().catch(() => null) as { detail?: unknown } | null;
  const detail = data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const text = detail
      .map(item => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && 'msg' in item) return String((item as { msg: unknown }).msg);
        return '';
      })
      .filter(Boolean)
      .join('；');
    if (text) return text;
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message;
  }
  return `请求失败（${response.status}）`;
}

async function send<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return response.json() as Promise<T>;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export async function listProjects(): Promise<ProjectRecord[]> {
  const data = await send<{ projects?: ProjectRecord[] }>('/api/library/projects');
  return data.projects || [];
}

export async function createEpisode(input: {
  name: string;
  idea?: string;
  budget_cap?: number | null;
  llm_model?: string;
  image_model?: string;
  video_model?: string;
  tts_model?: string;
}): Promise<EpisodeCreateResult> {
  const body: Record<string, unknown> = { name: input.name };
  if (input.idea) body.idea = input.idea;
  if (typeof input.budget_cap === 'number') body.budget_cap = input.budget_cap;
  for (const key of ['llm_model', 'image_model', 'video_model', 'tts_model'] as const) {
    const value = input[key];
    if (value && value.trim()) body[key] = value.trim();
  }
  return send<EpisodeCreateResult>('/api/v2/projects', jsonInit('POST', body));
}

export async function getProjectStatus(projectId: string): Promise<ProjectStatus> {
  return send<ProjectStatus>(`/api/v2/projects/${encodeURIComponent(projectId)}/status`);
}

export async function deleteProject(projectId: string): Promise<{ status: string; id: string }> {
  const result = await send<{ status: string; id: string }>(
    `/api/v2/projects/${encodeURIComponent(projectId)}`,
    { method: 'DELETE' },
  );
  if (result.status !== 'deleted' || result.id !== projectId) {
    throw new ApiError(500, '服务器没有确认项目已删除');
  }
  return result;
}

export async function stopProject(projectId: string): Promise<StopResult> {
  return send<StopResult>(`/api/v2/projects/${encodeURIComponent(projectId)}/stop`, { method: 'POST' });
}

export async function updateModels(
  projectId: string,
  models: { llm_model?: string; image_model?: string; video_model?: string; tts_model?: string },
): Promise<ModelUpdateResult> {
  const body: Record<string, string> = {};
  for (const [key, value] of Object.entries(models)) {
    if (typeof value === 'string' && value.trim()) body[key] = value.trim();
  }
  return send<ModelUpdateResult>(
    `/api/v2/projects/${encodeURIComponent(projectId)}/models`,
    jsonInit('PATCH', body),
  );
}

export async function updatePrompt(projectId: string, nodeId: string, text: string): Promise<{ text: string }> {
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/prompt`,
    jsonInit('PATCH', { text }),
  );
}

export interface FrameAsset {
  id: string;
  name: string;
}

export async function listFrameAssets(projectId: string): Promise<FrameAsset[]> {
  const query = new URLSearchParams({ project_id: projectId, include_global: 'true' });
  const [assets, images] = await Promise.all([
    send<{ assets?: Array<{ id?: string; name?: string }> }>(`/api/library/assets?${query.toString()}`),
    send<{ images?: Array<{ asset_id?: string | null }> }>('/api/library/images'),
  ]);
  const withImage = new Set(
    (images.images || [])
      .map(row => row.asset_id)
      .filter((id): id is string => typeof id === 'string' && id.length > 0),
  );
  return (assets.assets || [])
    .filter(asset => typeof asset.id === 'string' && withImage.has(asset.id))
    .map(asset => ({ id: asset.id as string, name: asset.name?.trim() || (asset.id as string) }));
}

export async function runNode(
  projectId: string,
  nodeId: string,
  body: Record<string, unknown>,
): Promise<NodeActionResult> {
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/run`,
    jsonInit('POST', body),
  );
}

export async function confirmNode(projectId: string, nodeId: string): Promise<NodeActionResult> {
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/confirm`,
    jsonInit('POST', confirmRequest()),
  );
}

export async function declineNode(projectId: string, nodeId: string): Promise<NodeActionResult> {
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/decline`,
    { method: 'POST' },
  );
}

export async function raiseBudget(projectId: string, entryId: string, budgetCap: number): Promise<NodeActionResult> {
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/budget/${encodeURIComponent(entryId)}/raise`,
    jsonInit('POST', { budget_cap: budgetCap }),
  );
}

export async function declineBudget(projectId: string, entryId: string): Promise<NodeActionResult> {
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/budget/${encodeURIComponent(entryId)}/decline`,
    { method: 'POST' },
  );
}

export async function retryVideoSubmit(
  projectId: string,
  nodeId: string,
  forceNew: boolean,
  secondConfirmed: boolean,
): Promise<NodeActionResult> {
  const body = retryRequest(forceNew, secondConfirmed);
  return send(
    `/api/v2/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/retry_video_submit`,
    jsonInit('POST', body),
  );
}
