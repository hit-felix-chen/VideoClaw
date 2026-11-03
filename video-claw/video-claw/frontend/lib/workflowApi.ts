/**
 * 工作流 API 客户端
 */

/**
 * Streaming endpoints must bypass the Next.js rewrite proxy because it buffers
 * the entire upstream response before forwarding, which breaks SSE real-time delivery.
 * Non-streaming endpoints can still go through the proxy (relative URL).
 */
export const DIRECT_API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';
const STREAM_API_BASE = DIRECT_API_BASE;

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

export async function fetchStages(): Promise<StageInfo[]> {
  const resp = await fetch('/api/stages');
  const data = await resp.json();
  return data.stages;
}

export async function fetchSessions(): Promise<any[]> {
  const resp = await fetch('/api/sessions');
  const data = await resp.json();
  return data.sessions || [];
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
  const formData = new FormData();
  formData.append('item_type', itemType);
  formData.append('item_id', itemId);
  formData.append('file', file);
  const resp = await fetch(`/api/project/${sessionId}/artifact/${stage}/upload_image`, {
    method: 'POST',
    body: formData,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '上传图片失败' }));
    throw new Error(err.detail || '上传图片失败');
  }
  return resp.json();
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
  const resp = await fetch('/api/project/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '项目创建失败' }));
    throw new Error(err.detail || '项目创建失败');
  }
  return resp.json();
}

export async function getProjectStatus(sessionId: string): Promise<ProjectStatus> {
  const resp = await fetch(`/api/project/${sessionId}/status`);
  if (!resp.ok) throw new Error('Failed to get project status');
  return resp.json();
}

// 兼容旧路由名；后端实际会通过统一的 workflow state 入口返回状态。
export async function getProjectStatusFromDisk(sessionId: string): Promise<any> {
  const resp = await fetch(`/api/project/${sessionId}/status/from_disk`);
  if (!resp.ok) throw new Error('Failed to get project status snapshot');
  return resp.json();
}

export async function getArtifact(sessionId: string, stage: string): Promise<any> {
  const resp = await fetch(`/api/project/${sessionId}/artifact/${stage}`);
  if (!resp.ok) throw new Error(`Artifact for stage '${stage}' not found`);
  return resp.json();
}

export async function checkSceneAssets(sessionId: string, sceneNumber: number): Promise<{
  scene_number: number;
  reference_images: number;
  videos: number;
  shot_count: number;
}> {
  const resp = await fetch(`/api/project/${sessionId}/scene/${sceneNumber}/assets`);
  if (!resp.ok) return { scene_number: sceneNumber, reference_images: 0, videos: 0, shot_count: 0 };
  return resp.json();
}

export async function executeStage(
  sessionId: string,
  stage: string,
  inputData: Record<string, any> = {},
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${STREAM_API_BASE}/api/project/${sessionId}/execute/${stage}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(inputData),
    signal,
  });
}

export async function intervene(
  sessionId: string,
  stage: string,
  modifications: Record<string, any>,
): Promise<Response> {
  // Use STREAM_API_BASE to bypass Next.js proxy (SSE endpoint)
  return fetch(`${STREAM_API_BASE}/api/project/${sessionId}/intervene`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stage, modifications }),
  });
}

export async function stopProject(sessionId: string): Promise<{ status: string }> {
  const resp = await fetch(`/api/project/${sessionId}/stop`, {
    method: 'POST',
  });
  return resp.json();
}

export async function updateModels(
  sessionId: string,
  models: Partial<Record<string, string | boolean>>,
): Promise<{ status: string }> {
  const resp = await fetch(`/api/project/${sessionId}/models`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(models),
  });
  return resp.json();
}

export async function deleteSession(
  sessionId: string,
): Promise<{ status: string }> {
  const resp = await fetch(`/api/sessions/${sessionId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '删除失败' }));
    throw new Error(err.detail || '删除失败');
  }
  return resp.json();
}

export async function saveSelections(
  sessionId: string,
  stage: string,
  selections: Record<string, any>,
): Promise<{ status: string }> {
  const resp = await fetch(`/api/project/${sessionId}/artifact/${stage}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(selections),
  });
  if (!resp.ok) throw new Error('保存选项失败');
  return resp.json();
}

export async function continueWorkflow(sessionId: string): Promise<{ status: string; next_stage?: string }> {
  const resp = await fetch(`/api/project/${sessionId}/continue`, {
    method: 'POST',
  });
  return resp.json();
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
