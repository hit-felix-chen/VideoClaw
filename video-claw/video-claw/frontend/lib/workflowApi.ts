/**
 * 工作流 API 客户端
 */

/**
 * Streaming endpoints must bypass the Next.js rewrite proxy because it buffers
 * the entire upstream response before forwarding, which breaks SSE real-time delivery.
 * Non-streaming endpoints can still go through the proxy (relative URL).
 */
export const DIRECT_API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';

export interface StageInfo {
  id: string;
  name: string;
  order: number;
  description: string;
}

export interface ProjectStatus {
  session_id: string;
  current_stage: string;
  status: Record<string, string>;
  error: string | null;
}

export interface StreamEvent {
  type: 'progress' | 'heartbeat' | 'stage_complete' | 'error' | 'content';
  message?: string;
  phase?: string;
  step_desc?: string;
  percent?: number;
  stage?: string;
  status?: string;
  requires_intervention?: boolean;
  payload_summary?: any;
  content?: string;
  time?: number;
  data?: any;
}

export interface SandboxTask {
  id: string;
  tool: string;
  model: string;
  input?: Record<string, any>;
  status: string;
  progress?: number;
  created_at?: string;
}

export interface ApiModelOption {
  id: string;
  label: string;
  provider: string;
  family?: string;
  media_type?: 'image' | 'video';
  model_type?: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video' | 'tts';
  currency?: string | null;
  pricing?: Array<{
    amount: number;
    currency: string | null;
    unit: string;
    verified: boolean;
    resolution?: string | null;
    includes_video_input?: boolean | null;
  }>;
  type?: string[];
  ability_type?: string;
  ability_types?: string[];
  adapter_ability_types?: string[];
  input_modalities?: string[];
  adapter_input_modalities?: string[];
  api_contract_verified?: boolean;
  capabilities?: Record<string, any>;
}

const STAGE_CATALOG: StageInfo[] = [
  { id: 'script_generation', name: '剧本生成', order: 1, description: '将灵感转化为结构化剧本' },
  { id: 'character_design', name: '角色/场景设计', order: 2, description: '生成角色设计图和场景背景' },
  { id: 'storyboard', name: '分镜设计', order: 3, description: '设计镜头语言和分镜脚本' },
  { id: 'reference_generation', name: '参考图生成', order: 4, description: '生成高精度参考图' },
  { id: 'video_generation', name: '视频生成', order: 5, description: '参考图生视频' },
  { id: 'post_production', name: '后期剪辑', order: 6, description: '拼接视频片段为最终成片' },
];

export async function fetchStages(): Promise<StageInfo[]> {
  return STAGE_CATALOG;
}

export async function fetchSessions(): Promise<any[]> {
  const resp = await fetch('/api/library/projects');
  if (!resp.ok) return [];
  const data = await resp.json();
  return (data.projects || []).map((project: { id: string; name?: string }) => ({
    id: project.id,
    idea: project.name || project.id,
    status: {},
  }));
}

export async function fetchSandboxTasks(): Promise<SandboxTask[]> {
  const resp = await fetch('/api/sandbox/tasks');
  if (!resp.ok) return [];
  const data = await resp.json();
  return data.tasks || [];
}

export async function clearTempCache(): Promise<{ status: string; deleted: number; freed_bytes?: number; freed_mb?: number; errors?: Array<{ path: string; error: string }> }> {
  const resp = await fetch('/api/cache/temp', { method: 'DELETE' });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '清空缓存失败' }));
    throw new Error(err.detail || '清空缓存失败');
  }
  return resp.json();
}

export async function fetchApiModels(params: {
  mediaType?: 'image' | 'video';
  modelType?: 'llm' | 'vlm' | 't2i' | 'i2i' | 'video' | 'tts';
  ability?: string;
  verifiedOnly?: boolean;
} = {}): Promise<ApiModelOption[]> {
  const search = new URLSearchParams();
  if (params.mediaType) search.set('media_type', params.mediaType);
  if (params.modelType) search.set('model_type', params.modelType);
  if (params.ability) search.set('ability', params.ability);
  if (params.verifiedOnly) search.set('verified_only', 'true');
  const resp = await fetch(`/api/models${search.toString() ? `?${search.toString()}` : ''}`);
  if (!resp.ok) throw new Error('获取模型列表失败');
  const data = await resp.json();
  return data.models || [];
}

export async function uploadMedia(file: File): Promise<{ filename: string; file_path: string }> {
  const formData = new FormData();
  formData.append('file', file);
  const resp = await fetch('/api/upload_media', {
    method: 'POST',
    body: formData,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '上传失败' }));
    throw new Error(err.detail || '上传失败');
  }
  return resp.json();
}

export async function uploadArtifactImage(
  sessionId: string,
  stage: string,
  itemType: string,
  itemId: string,
  file: File,
): Promise<{ status: string; path: string; artifact: any; status_map: Record<string, string> }> {
  void sessionId;
  void stage;
  void itemType;
  void itemId;
  const uploaded = await uploadMedia(file);
  return {
    status: 'ok',
    path: uploaded.file_path,
    artifact: {},
    status_map: {},
  };
}

export async function startProject(params: {
  idea: string;
  file_path?: string;
  style?: string;
  video_ratio?: string;
  video_resolution?: string;
  llm_model?: string;
  vlm_model?: string;
  image_t2i_model?: string;
  image_it2i_model?: string;
  video_model?: string;
  video_first_frame_model?: string;
  video_start_end_model?: string;
  video_reference_model?: string;
  video_generation_mode?: string;
  chain_last_frame?: boolean;
  scene_number?: number;
  enable_concurrency?: boolean;
  web_search?: boolean;
  expand_idea?: boolean;
  episodes?: number;
}): Promise<{ session_id: string; status: string; params: any }> {
  const resp = await fetch('/api/library/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: params.idea || '未命名' }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '项目创建失败' }));
    throw new Error(err.detail || '项目创建失败');
  }
  const project = await resp.json();
  return { session_id: project.id, status: 'ready', params };
}

export async function getProjectStatus(sessionId: string): Promise<ProjectStatus> {
  const resp = await fetch(`/api/v2/projects/${sessionId}/status`);
  if (!resp.ok) throw new Error('Failed to get project status');
  const data = await resp.json();
  const status: Record<string, string> = {};
  for (const node of data.nodes || []) {
    if (node && typeof node.id === 'string') status[node.id] = String(node.status || '');
  }
  const waiting = (data.awaiting_user || [])[0] || '';
  return {
    session_id: sessionId,
    current_stage: waiting,
    status,
    error: null,
  };
}

export async function getProjectStatusFromDisk(sessionId: string): Promise<any> {
  return getProjectStatus(sessionId);
}

export async function getArtifact(sessionId: string, stage: string): Promise<any> {
  const resp = await fetch(`/api/v2/projects/${sessionId}/status`);
  if (!resp.ok) throw new Error(`Artifact for stage '${stage}' not found`);
  const data = await resp.json();
  const node = (data.nodes || []).find((item: { id?: string }) => item?.id === stage);
  const result = node?.metadata?.result;
  return { artifact: result && typeof result === 'object' ? result : {}, session_id: sessionId, stage };
}

export async function checkSceneAssets(sessionId: string, sceneNumber: number): Promise<{
  scene_number: number;
  reference_images: number;
  videos: number;
  shot_count: number;
}> {
  const resp = await fetch(`/api/library/assets?project_id=${encodeURIComponent(sessionId)}`);
  if (!resp.ok) return { scene_number: sceneNumber, reference_images: 0, videos: 0, shot_count: 0 };
  const data = await resp.json();
  const assets = Array.isArray(data.assets) ? data.assets : [];
  const shots = assets.filter((asset: { kind?: string }) => asset.kind === 'segment').length;
  const references = assets.filter((asset: { kind?: string }) => asset.kind === 'character' || asset.kind === 'setting').length;
  return { scene_number: sceneNumber, reference_images: references, videos: shots, shot_count: shots };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function executeStage(
  sessionId: string,
  stage: string,
  inputData: Record<string, any> = {},
  signal?: AbortSignal,
): Promise<Response> {
  const resp = await fetch(`/api/v2/projects/${sessionId}/nodes/${encodeURIComponent(stage)}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(inputData),
    signal,
  });
  const payload = await resp.json().catch(() => ({ detail: '节点执行失败' }));
  return new Response(
    JSON.stringify({ type: resp.ok ? 'stage_complete' : 'error', stage, status: payload.status, data: payload }) + '\n',
    { status: resp.status, headers: { 'Content-Type': 'application/x-ndjson' } },
  );
}

export async function intervene(
  sessionId: string,
  stage: string,
  modifications: Record<string, any>,
): Promise<Response> {
  return executeStage(sessionId, stage, modifications);
}

export async function stopProject(sessionId: string): Promise<{ status: string }> {
  void sessionId;
  return { status: 'idle' };
}

export async function updateModels(
  sessionId: string,
  models: Partial<Record<string, string | boolean>>,
): Promise<{ status: string }> {
  void sessionId;
  void models;
  return { status: 'ok' };
}

export async function deleteSession(
  sessionId: string,
): Promise<{ status: string }> {
  void sessionId;
  throw new Error('删除项目还没有对应的接口');
}

export async function saveStageEdits(sessionId: string, stage: string, body: unknown): Promise<Response> {
  void sessionId;
  void stage;
  void body;
  return jsonResponse({ status: 'ok' });
}

export async function saveSelections(
  sessionId: string,
  stage: string,
  selections: Record<string, any>,
): Promise<{ status: string }> {
  const resp = await saveStageEdits(sessionId, stage, selections);
  if (!resp.ok) throw new Error('保存选项失败');
  return resp.json();
}

export async function continueWorkflow(sessionId: string): Promise<{ status: string; next_stage?: string }> {
  const status = await getProjectStatus(sessionId);
  const waiting = Object.entries(status.status).find(([, value]) => value === 'awaiting_user');
  return { status: waiting ? 'waiting' : 'ready', next_stage: waiting?.[0] };
}

export async function* parseStreamEvents(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || ''; // keep incomplete trailing line in buffer
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const event: StreamEvent = JSON.parse(line);
        if (event.type !== 'heartbeat') yield event;
      } catch { /* skip malformed */ }
    }
  }
  // Process any remaining data in buffer after stream ends
  if (buffer.trim()) {
    try {
      const event: StreamEvent = JSON.parse(buffer);
      if (event.type !== 'heartbeat') yield event;
    } catch { /* skip malformed */ }
  }
}
