'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { ChevronLeft, ChevronRight, Clock, Hexagon, Home, Loader2, PanelLeftOpen, Settings, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { useEffect, useState, type CSSProperties } from 'react';
import { clearTempCache, fetchSandboxTasks, type SandboxTask } from '@/lib/workflowApi';

const NAV_ITEMS = [
  { href: '/', label: 'Video-Claw', icon: Home },
  { href: '/sandbox', label: '临时工作台', icon: Hexagon },
];

const SETTINGS_ITEM = { href: '/settings', label: '设置', icon: Settings };

const TASK_STATUS_STYLE: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-500',
  running: 'bg-blue-50 text-blue-600',
  waiting: 'bg-amber-50 text-amber-600',
  completed: 'bg-green-50 text-green-600',
  failed: 'bg-red-50 text-red-600',
};

function statusText(status?: string) {
  if (status === 'pending') return '等待中';
  if (status === 'running') return '生成中';
  if (status === 'waiting') return '待确认';
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  return status || '未知';
}

type RunningTaskItem = {
  id: string;
  href: string;
  title: string;
  scope: string;
  status: string;
  progress: number;
};

const SIDEBAR_OPEN_KEY = 'video-claw.sidebar-open';

const SANDBOX_TOOL_LABELS: Record<string, string> = {
  llm: 'LLM',
  vlm: 'VLM',
  t2i: '文生图',
  i2i: '图生图',
  video: '视频生成',
};

function sandboxTaskItem(task: SandboxTask): RunningTaskItem {
  const input = task.input || {};
  return {
    id: `sandbox-${task.id}`,
    href: `/sandbox?task=${encodeURIComponent(task.id)}`,
    title: input.prompt || input.reference_image || task.id,
    scope: `临时工作台 · ${SANDBOX_TOOL_LABELS[task.tool] || task.tool}`,
    status: task.status || 'running',
    progress: task.progress || 1,
  };
}

function loadSidebarOpen(): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(SIDEBAR_OPEN_KEY) === 'true';
}

function saveSidebarOpen(open: boolean) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(SIDEBAR_OPEN_KEY, open ? 'true' : 'false');
}

function TaskPanel({
  title,
  icon,
  loading,
  tasks,
  currentPath,
  emptyText,
  onTaskClick,
}: {
  title: string;
  icon: React.ReactNode;
  loading?: boolean;
  tasks: RunningTaskItem[];
  currentPath: string;
  emptyText: string;
  onTaskClick: (task: RunningTaskItem) => void;
}) {
  return (
    <section className="h-[20vh] min-h-32 border-t border-gray-100 p-3">
      <div className="mb-2 flex items-center gap-2 px-1">
        {icon}
        <span className="text-xs font-medium text-gray-500">{title}</span>
        {loading && <Loader2 className="ml-auto h-3 w-3 animate-spin text-gray-300" />}
      </div>
      <div className="h-[calc(100%-26px)] overflow-y-auto pr-1">
        {tasks.length ? (
          <div className="space-y-1.5">
            {tasks.map(task => {
              const active = currentPath === task.href.split('?')[0];
              return (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => onTaskClick(task)}
                  className={clsx(
                    'w-full rounded-lg border px-2.5 py-2 text-left transition-colors',
                    active ? 'border-blue-100 bg-blue-50/60' : 'border-gray-100 bg-white hover:border-blue-200 hover:bg-blue-50/40'
                  )}
                >
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-700">
                      {task.title.slice(0, 36)}
                    </span>
                    <span className={clsx('flex-shrink-0 rounded px-1.5 py-0.5 text-[10px]', TASK_STATUS_STYLE[task.status] || TASK_STATUS_STYLE.pending)}>
                      {statusText(task.status)}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <span className="truncate text-[10px] text-gray-400">{task.scope}</span>
                    <div className="h-1 min-w-10 flex-1 overflow-hidden rounded-full bg-gray-100">
                      <div className="h-full rounded-full bg-blue-500" style={{ width: `${task.progress || 0}%` }} />
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        ) : (
          <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-gray-100 px-2 text-center text-xs text-gray-300">
            {emptyText}
          </div>
        )}
      </div>
    </section>
  );
}

function SidebarTaskPanels({ currentPath }: { currentPath: string }) {
  const router = useRouter();
  const [runningTasks, setRunningTasks] = useState<RunningTaskItem[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const sandboxRecords = await fetchSandboxTasks().catch(() => []);
      setRunningTasks(sandboxRecords.map(sandboxTaskItem));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load().catch(() => {});
    const timer = window.setInterval(() => load().catch(() => {}), 3000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <TaskPanel
      title="临时工作台"
      icon={<Clock className="h-3.5 w-3.5 text-gray-400" />}
      loading={loading}
      tasks={runningTasks}
      currentPath={currentPath}
      emptyText="暂无进行中的临时任务"
      onTaskClick={task => router.push(task.href)}
    />
  );
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false);
  const [clearingCache, setClearingCache] = useState(false);

  useEffect(() => {
    setOpen(loadSidebarOpen());
  }, []);

  const setSidebarOpen = (nextOpen: boolean) => {
    setOpen(nextOpen);
    saveSidebarOpen(nextOpen);
  };

  const handleClearCache = async () => {
    setClearingCache(true);
    try {
      const result = await clearTempCache();
      window.alert(`缓存已清空，删除 ${result.deleted} 项，释放 ${Number(result.freed_mb || 0).toFixed(2)} MB。`);
      setSettingsMenuOpen(false);
    } catch (error: any) {
      window.alert(error?.message || '清空缓存失败');
    } finally {
      setClearingCache(false);
    }
  };

  return (
    <div
      className="min-h-screen bg-gray-50 text-gray-800"
      style={{ '--app-sidebar-width': open ? '15rem' : '0px' } as CSSProperties}
    >
      <aside
        className={clsx(
          'fixed inset-y-0 left-0 z-40 border-r border-gray-200 bg-white shadow-sm transition-all duration-300',
          open ? 'w-60' : 'w-0 border-r-0'
        )}
      >
        <div className={clsx('flex h-full flex-col overflow-hidden transition-opacity duration-200', open ? 'opacity-100' : 'opacity-0')}>
          <div className="flex h-16 items-center px-4 border-b border-gray-100">
            <div className="flex items-center gap-2 min-w-0">
              <PanelLeftOpen className="w-4 h-4 text-blue-500 flex-shrink-0" />
              <span className="text-sm font-semibold text-gray-800 truncate">Video-Claw</span>
            </div>
          </div>
          <nav className="min-h-0 flex-1 overflow-y-auto p-3 space-y-1">
            {NAV_ITEMS.map(item => {
              const Icon = item.icon;
              const active = item.href === '/' ? pathname === '/' : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={clsx(
                    'flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors',
                    active
                      ? 'bg-blue-50 text-blue-600'
                      : 'text-gray-500 hover:bg-gray-50 hover:text-gray-800'
                  )}
                >
                  <Icon className="w-4 h-4 flex-shrink-0" />
                  <span className="truncate">{item.label}</span>
                </Link>
              );
            })}
          </nav>
          <SidebarTaskPanels currentPath={pathname} />
          <div className="relative border-t border-gray-100 p-3">
            {(() => {
              const Icon = SETTINGS_ITEM.icon;
              const active = pathname.startsWith(SETTINGS_ITEM.href);
              return (
                <>
                  {settingsMenuOpen && (
                    <div className="absolute bottom-[62px] left-3 right-3 rounded-xl border border-gray-200 bg-white p-2 shadow-lg">
                      <button
                        type="button"
                        onClick={() => {
                          setSettingsMenuOpen(false);
                          router.push(SETTINGS_ITEM.href);
                        }}
                        className="flex h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium text-gray-600 hover:bg-blue-50 hover:text-blue-600"
                      >
                        <Settings className="h-4 w-4" />
                        修改配置
                      </button>
                      <button
                        type="button"
                        onClick={() => handleClearCache()}
                        disabled={clearingCache}
                        className="mt-1 flex h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium text-gray-600 hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        {clearingCache ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                        清空缓存
                      </button>
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => setSettingsMenuOpen(value => !value)}
                  className={clsx(
                    'flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors',
                    active || settingsMenuOpen
                      ? 'bg-blue-50 text-blue-600'
                      : 'text-gray-500 hover:bg-gray-50 hover:text-gray-800'
                  )}
                >
                  <Icon className="w-4 h-4 flex-shrink-0" />
                  <span className="truncate">{SETTINGS_ITEM.label}</span>
                  </button>
                </>
              );
            })()}
          </div>
        </div>
      </aside>

      {open && (
        <button
          onClick={() => setSidebarOpen(false)}
          className="fixed left-60 top-1/2 z-50 h-14 w-7 -translate-y-1/2 rounded-r-xl border border-l-0 border-gray-200 bg-white text-gray-400 shadow-sm hover:w-9 hover:text-blue-600 hover:border-blue-200 hover:bg-blue-50 flex items-center justify-center transition-all"
          title="收起侧边栏"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
      )}

      {!open && (
        <button
          onClick={() => setSidebarOpen(true)}
          className="fixed left-0 top-1/2 z-50 h-14 w-7 -translate-y-1/2 rounded-r-xl border border-l-0 border-gray-200 bg-white text-gray-400 shadow-sm hover:w-9 hover:text-blue-600 hover:border-blue-200 hover:bg-blue-50 flex items-center justify-center transition-all"
          title="打开侧边栏"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      )}

      <main className={clsx('min-h-screen min-w-0 overflow-x-hidden transition-[margin] duration-300', open ? 'ml-60' : 'ml-0')}>
        {children}
      </main>
    </div>
  );
}
