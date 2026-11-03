/**
 * 仍在使用的通用 API：模型列表、上传、临时工作台和缓存。
 * 一集的依赖图走 lib/episodeApi.ts。
 */

export const DIRECT_API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';

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
    throw new Error(typeof err.detail === 'string' ? err.detail : '清空缓存失败');
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
    throw new Error(typeof err.detail === 'string' ? err.detail : '上传失败');
  }
  return resp.json();
}
