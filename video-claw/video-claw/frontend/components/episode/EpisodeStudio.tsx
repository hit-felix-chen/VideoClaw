'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import NodeCard from '@/components/episode/NodeCard';
import {
  createEpisode,
  declineBudget,
  declineNode,
  deleteProject,
  errorMessage,
  getProjectStatus,
  listProjects,
  raiseBudget,
  retryVideoSubmit,
  runNode,
  stopProject,
  updateModels,
  updatePrompt,
  confirmNode,
  type GraphNode,
  type ModelUpdateResult,
  type NodeActionResult,
  type ProjectRecord,
  type ProjectStatus,
  type StopResult,
} from '@/lib/episodeApi';
import { confirmThenReload, mergeModelDrafts, nodeLabel, runPayload, statusLabel } from '@/lib/episodeFlow';

const EMPTY_MODELS = { llm_model: '', image_model: '', video_model: '', tts_model: '' };

function modelsFromNodes(nodes: GraphNode[]) {
  const pick = (kinds: string[]) => {
    const found = nodes.find(node => kinds.includes(node.kind));
    const value = found?.metadata?.model_id;
    return typeof value === 'string' ? value : '';
  };
  return {
    llm_model: pick(['script', 'storyboard', 'previz']),
    image_model: pick(['character', 'keyframe', 'scene', 'image']),
    video_model: pick(['shot_segment', 'draft_video', 'final_video', 'video']),
    tts_model: pick(['tts']),
  };
}

function promptOf(node: GraphNode): string {
  const text = node.metadata?.text;
  return typeof text === 'string' ? text : '';
}

export default function EpisodeStudio() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const projectId = searchParams.get('project');

  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [models, setModels] = useState(EMPTY_MODELS);
  const modelBaseline = useRef(EMPTY_MODELS);
  const [name, setName] = useState('');
  const [idea, setIdea] = useState('');
  const [budget, setBudget] = useState('');
  const [createModels, setCreateModels] = useState(EMPTY_MODELS);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [actions, setActions] = useState<Record<string, NodeActionResult>>({});
  const [confirmDelete, setConfirmDelete] = useState(false);

  const applyStatus = useCallback((next: ProjectStatus) => {
    setStatus(next);
    setDrafts(current => {
      const merged = { ...current };
      for (const node of next.nodes || []) {
        if (merged[node.id] === undefined) merged[node.id] = promptOf(node);
      }
      return merged;
    });
    const incoming = modelsFromNodes(next.nodes || []);
    setModels(current => {
      const merged = mergeModelDrafts(current, modelBaseline.current, incoming);
      modelBaseline.current = merged.baseline;
      return merged.drafts;
    });
  }, []);

  const reload = useCallback(async (id: string) => {
    const next = await getProjectStatus(id);
    applyStatus(next);
    return next;
  }, [applyStatus]);

  const refreshProjects = useCallback(async () => {
    setProjects(await listProjects());
  }, []);

  useEffect(() => {
    refreshProjects().catch(reason => setError(errorMessage(reason)));
  }, [refreshProjects]);

  useEffect(() => {
    if (!projectId) {
      setStatus(null);
      return;
    }
    modelBaseline.current = EMPTY_MODELS;
    setModels(EMPTY_MODELS);
    let cancelled = false;
    setError('');
    getProjectStatus(projectId)
      .then(next => {
        if (!cancelled) applyStatus(next);
      })
      .catch(reason => {
        if (!cancelled) {
          setStatus(null);
          setError(errorMessage(reason));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, applyStatus]);

  const running = useMemo(
    () => (status?.nodes || []).some(node => node.status === 'running' || node.node_state === 'running'),
    [status],
  );

  useEffect(() => {
    if (!projectId || !running) return;
    const timer = window.setInterval(() => {
      getProjectStatus(projectId).then(applyStatus).catch(reason => setError(errorMessage(reason)));
    }, 3000);
    return () => window.clearInterval(timer);
  }, [projectId, running, applyStatus]);

  const openProject = (id: string) => {
    setNotice('');
    setError('');
    setActions({});
    setConfirmDelete(false);
    router.push(`/?project=${encodeURIComponent(id)}`);
  };

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await work();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const onCreate = () => run(async () => {
    const cap = budget.trim() === '' ? null : Number(budget);
    if (cap !== null && Number.isNaN(cap)) {
      setError('预算上限必须是数字');
      return;
    }
    const created = await createEpisode({
      name: name.trim() || idea.trim() || '未命名',
      idea: idea.trim(),
      budget_cap: cap,
      ...createModels,
    });
    if (!created.project?.id || !Array.isArray(created.nodes) || created.nodes.length === 0) {
      setError('项目已创建，但服务器没有返回依赖图');
      await refreshProjects();
      return;
    }
    await refreshProjects();
    setDrafts({});
    openProject(created.project.id);
    const loaded = await reload(created.project.id);
    if (!loaded.nodes?.length) setError('依赖图没有保存，刷新后读不到节点');
  });

  const remember = (nodeId: string, result: NodeActionResult) => {
    setActions(current => ({ ...current, [nodeId]: { ...result, node_id: result.node_id || nodeId } }));
    if (result.message) setNotice(result.message);
    const bad = result.status === 'blocked' || result.status === 'unavailable' || result.status === 'unsupported' || result.status === 'failed';
    if (bad) setError(result.message || `节点状态：${statusLabel(result.status)}`);
  };

  const onRun = (node: GraphNode) => run(async () => {
    if (!projectId) return;
    const result = await runNode(projectId, node.id, runPayload(node));
    remember(node.id, result);
    await reload(projectId);
  });

  const onSavePrompt = (node: GraphNode) => run(async () => {
    if (!projectId) return;
    const text = drafts[node.id] ?? '';
    const saved = await updatePrompt(projectId, node.id, text);
    const loaded = await reload(projectId);
    const stored = loaded.nodes.find(item => item.id === node.id);
    const roundTrip = typeof stored?.metadata?.text === 'string' ? stored.metadata.text : '';
    if (saved.text !== text || roundTrip !== text) {
      setError('提示词没有按提交的内容保存');
      return;
    }
    setDrafts(current => ({ ...current, [node.id]: roundTrip }));
    setNotice('提示词已保存');
  });

  const onModels = () => run(async () => {
    if (!projectId) return;
    const result: ModelUpdateResult = await updateModels(projectId, models);
    const loaded = await reload(projectId);
    const missed = result.updated.filter(item => {
      const node = loaded.nodes.find(candidate => candidate.id === item.node_id);
      return node?.metadata?.model_id !== item.model_id;
    });
    if (missed.length) {
      setError(`模型没有写入：${missed.map(item => item.node_id).join('、')}`);
      return;
    }
    const blocked = result.blocked.map(item => `${item.node_id}：${item.reason}`).join('；');
    setNotice(blocked ? `已保存部分模型。未更换：${blocked}` : '模型已保存');
  });

  const onStop = () => run(async () => {
    if (!projectId) return;
    const result: StopResult = await stopProject(projectId);
    await reload(projectId);
    const refused = result.refused.map(item => `${item.node_id}：${item.reason}`).join('；');
    if (!result.stopped.length) {
      setNotice(refused ? `${result.message}。${refused}` : result.message);
      return;
    }
    setNotice(refused ? `${result.message}。未停止：${refused}` : result.message);
  });

  const onDelete = () => run(async () => {
    if (!projectId) return;
    await deleteProject(projectId);
    setConfirmDelete(false);
    setStatus(null);
    await refreshProjects();
    router.push('/');
    setNotice('项目已删除');
  });

  const nodes = status?.nodes || [];
  const edges = status?.edges || [];
  const executable = new Set(status?.executable || []);
  const current = projects.find(project => project.id === projectId);

  return (
    <div className="mx-auto grid min-h-screen max-w-6xl gap-6 px-4 py-6 lg:grid-cols-[16rem_1fr]">
      <aside className="space-y-4">
        <section className="space-y-2 rounded-xl border border-gray-200 bg-white p-3">
          <h2 className="text-sm font-semibold text-gray-800">新建一集</h2>
          <input value={name} onChange={event => setName(event.target.value)} placeholder="项目名称" className="w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm" />
          <textarea value={idea} onChange={event => setIdea(event.target.value)} placeholder="这一集的想法，会写入各节点提示词" rows={4} className="w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm" />
          <input value={budget} onChange={event => setBudget(event.target.value)} placeholder="预算上限（可空）" className="w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm" />
          {([
            ['llm_model', '剧本模型'],
            ['image_model', '图像模型'],
            ['video_model', '视频模型'],
            ['tts_model', '配音模型'],
          ] as const).map(([key, label]) => (
            <input
              key={key}
              value={createModels[key]}
              onChange={event => setCreateModels(currentModels => ({ ...currentModels, [key]: event.target.value }))}
              placeholder={label}
              className="w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm"
            />
          ))}
          <button type="button" disabled={busy} onClick={onCreate} className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50">
            创建并生成依赖图
          </button>
        </section>
        <section className="space-y-1">
          <h2 className="px-1 text-xs font-medium text-gray-500">项目</h2>
          {projects.length === 0 && <p className="px-1 text-xs text-gray-400">还没有项目</p>}
          {projects.map(project => (
            <button
              key={project.id}
              type="button"
              onClick={() => openProject(project.id)}
              className={`block w-full truncate rounded-lg px-2 py-1.5 text-left text-sm ${project.id === projectId ? 'bg-blue-50 text-blue-700' : 'text-gray-700 hover:bg-gray-100'}`}
            >
              {project.name || project.id}
            </button>
          ))}
        </section>
      </aside>

      <main className="space-y-4">
        {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
        {notice && <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">{notice}</div>}

        {!projectId && <p className="text-sm text-gray-500">创建一个项目后，这里会列出这一集的依赖图和节点。</p>}

        {projectId && status && (
          <>
            <header className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-semibold text-gray-900">{current?.name || projectId}</h1>
              <span className="text-xs text-gray-500">
                预算 {status.budget?.cap ?? '不限'}
                {status.budget?.currency ? ` ${status.budget.currency}` : ''}
                {typeof status.budget?.used === 'number' ? `，已用 ${status.budget.used}` : ''}
              </span>
              <button type="button" disabled={busy} onClick={() => projectId && run(() => reload(projectId).then(() => undefined))} className="rounded-lg border border-gray-200 px-2 py-1 text-xs text-gray-600 disabled:opacity-50">刷新</button>
              <button type="button" disabled={busy} onClick={onStop} className="rounded-lg border border-gray-200 px-2 py-1 text-xs text-gray-700 disabled:opacity-50">停止</button>
              <button type="button" disabled={busy} onClick={() => setConfirmDelete(true)} className="rounded-lg border border-red-200 px-2 py-1 text-xs text-red-600 disabled:opacity-50">删除项目</button>
            </header>

            {confirmDelete && (
              <div role="dialog" aria-label="确认删除" className="flex flex-wrap items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
                <span>确认删除这个项目？删除后无法恢复。</span>
                <button type="button" disabled={busy} onClick={onDelete} className="rounded-lg bg-red-600 px-3 py-1 text-white disabled:opacity-50">确认删除</button>
                <button type="button" onClick={() => setConfirmDelete(false)} className="rounded-lg border border-red-200 bg-white px-3 py-1">取消</button>
              </div>
            )}

            <section className="space-y-2 rounded-xl border border-gray-200 bg-white p-4">
              <h2 className="text-sm font-semibold text-gray-800">模型</h2>
              <div className="grid gap-2 sm:grid-cols-2">
                {([
                  ['llm_model', '剧本 / 分镜'],
                  ['image_model', '角色 / 参考图'],
                  ['video_model', '视频'],
                  ['tts_model', '配音'],
                ] as const).map(([key, label]) => (
                  <label key={key} className="text-xs text-gray-500">
                    {label}
                    <input
                      value={models[key]}
                      onChange={event => setModels(currentModels => ({ ...currentModels, [key]: event.target.value }))}
                      className="mt-1 w-full rounded-lg border border-gray-200 px-2 py-1.5 text-sm text-gray-800"
                    />
                  </label>
                ))}
              </div>
              <button type="button" disabled={busy} onClick={onModels} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-700 disabled:opacity-50">保存模型</button>
            </section>

            <section className="space-y-3">
              <h2 className="text-sm font-semibold text-gray-800">依赖图</h2>
              {nodes.length === 0 && <p className="text-sm text-red-600">这个项目没有依赖图。</p>}
              {edges.length > 0 && (
                <p className="text-xs leading-6 text-gray-500">
                  {edges.map(edge => `${nodeLabel(nodes.find(node => node.id === edge.predecessor) || { id: edge.predecessor })} → ${nodeLabel(nodes.find(node => node.id === edge.successor) || { id: edge.successor })}`).join('    ')}
                </p>
              )}
              <div className="space-y-3">
                {nodes.map(node => (
                  <NodeCard
                    key={node.id}
                    node={node}
                    executable={executable.has(node.id)}
                    busy={busy}
                    draft={drafts[node.id] ?? promptOf(node)}
                    onDraft={text => setDrafts(currentDrafts => ({ ...currentDrafts, [node.id]: text }))}
                    onSavePrompt={() => onSavePrompt(node)}
                    onRun={() => onRun(node)}
                    onConfirm={() => run(async () => {
                      if (!projectId) return;
                      const result = await confirmThenReload(
                        () => confirmNode(projectId, node.id),
                        () => reload(projectId),
                      );
                      remember(node.id, result);
                    })}
                    onDecline={() => run(async () => {
                      if (!projectId) return;
                      const result = await declineNode(projectId, node.id);
                      remember(node.id, result);
                      await reload(projectId);
                    })}
                    onRaise={cap => run(async () => {
                      if (!projectId || !node.entry_id) return;
                      const result = await raiseBudget(projectId, node.entry_id, cap);
                      remember(node.id, result);
                      await reload(projectId);
                    })}
                    onDeclineBudget={() => run(async () => {
                      if (!projectId || !node.entry_id) return;
                      const result = await declineBudget(projectId, node.entry_id);
                      remember(node.id, result);
                      await reload(projectId);
                    })}
                    ledger={status.ledger}
                    onRetry={forceNew => run(async () => {
                      if (!projectId) return;
                      const result = await retryVideoSubmit(projectId, node.id, forceNew, forceNew);
                      remember(node.id, result);
                      await reload(projectId);
                    })}
                    action={actions[node.id] || null}
                  />
                ))}
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
