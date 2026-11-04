"""
Seedance 视频生成 API 客户端 (字节跳动 ARK)

"""

import os
import sys

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import time
import logging
import requests
import base64
from typing import Optional
from config import Config

logger = logging.getLogger(__name__)

# 120 polls * (30s GET timeout + 5s pause) plus the create and download
# timeouts is about 74 minutes. The executor's claim TTL is 30 minutes, so
# the whole submit must give up sooner than that and return the node to
# the caller instead of being reclaimed while it is still polling.
SUBMIT_DEADLINE_SECONDS = 20 * 60
# The cancel and query pages do not document a client timeout. This process
# treats a single GET or DELETE that exceeds 30 seconds as an uncertain
# result and does not release a quote.
CANCEL_HTTP_TIMEOUT_SECONDS = 30
_TASK_STATUSES = frozenset({"queued", "running", "cancelled", "succeeded", "failed", "expired"})


def _redact_logged_body(text: str) -> str:
    """Hide the API key if a vendor error body echoes it."""
    key = Config.ARK_API_KEY
    if isinstance(text, str) and isinstance(key, str) and key and key in text:
        return text.replace(key, "***")
    return text if isinstance(text, str) else ""


def _response_task_id(resp) -> Optional[str]:
    """Task id carried on a non-OK create response, if the body has one."""
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    task_id = data.get("id")
    if isinstance(task_id, str) and task_id.strip():
        return task_id.strip()
    return None


class SeedanceVideoClient:
    """
    Seedance 视频生成客户端（字节跳动 ARK）
    支持图生视频功能，采用 提交任务 -> 轮询 -> 下载 的异步流程
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key or Config.ARK_API_KEY
        self.base_url = (base_url or Config.ARK_BASE_URL or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
        self.timeout = timeout

        if not self.api_key:
            logger.warning("SeedanceVideoClient: ARK_API_KEY 未设置")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate_video(
        self,
        prompt: str,
        image_path: Optional[str],
        save_path: str,
        model: str = "doubao-seedance-2-0-260128",
        duration: int = 5,
        **kwargs
    ) -> str:
        """
        图生视频完整流程

        Args:
            prompt: 提示词
            image_path: 输入图片本地路径
            save_path: 输出视频保存路径
            model: 模型名称
            duration: 视频时长
        """
        if not self.api_key:
            raise RuntimeError("ARK_API_KEY not set.")

        deadline = time.monotonic() + SUBMIT_DEADLINE_SECONDS
        on_task = kwargs.pop("on_task", None)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Seedance 视频生成超时")
        kwargs["post_timeout"] = min(self.timeout, remaining)
        # 1. 提交任务。POST 的超时不能超过整次 submit 还剩的时间。
        # 4xx 也可能已经创建了任务。响应体里有 id 时先交给 on_task，再抛错。
        try:
            task_id = self._submit_task(prompt, image_path, model, duration, **kwargs)
        except Exception as exc:
            leaked = getattr(exc, "task_id", None)
            if callable(on_task) and isinstance(leaked, str) and leaked.strip():
                on_task(leaked.strip())
            raise
        if callable(on_task):
            on_task(task_id)
        self._check_deadline(deadline, task_id)

        # 2. 轮询等待。到总时限后自己失败，不等到 74 分钟被外部抢占。
        video_url = self._poll_until_done(task_id, deadline=deadline)

        # 3. 下载视频
        self._check_deadline(deadline, task_id)
        remaining = max(1, int(deadline - time.monotonic()))
        self._download_video(video_url, save_path, timeout=min(120, remaining))

        return video_url

    def resume(self, task_id: str, save_path: str) -> str:
        """Poll an existing task and download it. Does not create a new generation."""
        if not self.api_key:
            raise RuntimeError("ARK_API_KEY not set.")
        deadline = time.monotonic() + SUBMIT_DEADLINE_SECONDS
        video_url = self._poll_until_done(task_id, deadline=deadline)
        self._check_deadline(deadline, task_id)
        remaining = max(1, int(deadline - time.monotonic()))
        self._download_video(video_url, save_path, timeout=min(120, remaining))
        return video_url

    @staticmethod
    def image_url(image_path: str) -> str:
        if not image_path:
            raise FileNotFoundError("输入图片不存在: empty path")
        if image_path.startswith(("data:", "http://", "https://", "asset://")):
            return image_path
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"输入图片不存在: {image_path}")
        with open(image_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
        mime = "image/png" if ext == "png" else "image/jpeg"
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def image_content(image_path: str, role: str) -> dict:
        return {
            "type": "image_url",
            "image_url": {"url": SeedanceVideoClient.image_url(image_path)},
            "role": role,
        }

    @staticmethod
    def build_content(
        prompt: str,
        image_path: Optional[str] = None,
        reference_image_paths: Optional[list] = None,
        last_image_path: Optional[str] = None,
    ) -> list:
        """Ark content list. Reference images and first/last frame are mutually exclusive."""
        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        references = [path for path in (reference_image_paths or []) if path]
        if references:
            for path in references:
                content.append(SeedanceVideoClient.image_content(path, "reference_image"))
            return content
        if image_path:
            content.append(SeedanceVideoClient.image_content(image_path, "first_frame"))
        if last_image_path:
            content.append(SeedanceVideoClient.image_content(last_image_path, "last_frame"))
        return content

    def _submit_task(self, prompt: str, image_path: Optional[str], model: str, duration: int, **kwargs) -> str:
        # 根据 Seedance 2.0 文档更新接口路径
        url = f"{self.base_url}/contents/generations/tasks"
        content = self.build_content(
            prompt,
            image_path=image_path,
            reference_image_paths=kwargs.get("reference_image_paths"),
            last_image_path=kwargs.get("last_image_path"),
        )

        payload = {
            "model": model,
            "content": content,
            "duration": duration,
            "ratio": kwargs.get("ratio", "adaptive"),
            "resolution": kwargs.get("resolution", "720p")
        }

        # 合并其他可选参数 (如 seed, watermark)
        for key in ["seed", "watermark", "generate_audio"]:
            if key in kwargs:
                payload[key] = kwargs[key]

        logger.info(f"SeedanceVideoClient: 提交任务 model={model}, duration={duration}s")
        post_timeout = kwargs.pop("post_timeout", None)
        timeout = self.timeout if post_timeout is None else min(self.timeout, post_timeout)
        resp = requests.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=timeout,
            proxies=Config.requests_proxies("ark"),
        )
        
        if not resp.ok:
            logger.error("Seedance 提交失败: %s", _redact_logged_body(resp.text))
            task_id = _response_task_id(resp)
            try:
                resp.raise_for_status()
            except Exception as exc:
                if task_id:
                    exc.task_id = task_id
                raise
            
        data = resp.json()
        task_id = data.get("id")
        if not task_id:
            raise RuntimeError(f"Seedance API 未返回任务 ID: {data}")
            
        return task_id

    def poll_task(self, task_id: str) -> str:
        return self._poll_until_done(task_id)

    def download(self, url: str, save_path: str) -> str:
        self._download_video(url, save_path)
        return save_path

    @staticmethod
    def _check_deadline(deadline: float, task_id: str) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Seedance 视频生成超时 (task_id={task_id})")

    def _poll_until_done(
        self,
        task_id: str,
        max_polls: int = 120,
        interval: int = 5,
        deadline: Optional[float] = None,
    ) -> str:
        # 同步更新查询接口路径。deadline 是整次 submit 的单调时钟上限。
        if deadline is None:
            deadline = time.monotonic() + SUBMIT_DEADLINE_SECONDS
        url = f"{self.base_url}/contents/generations/tasks/{task_id}"

        for i in range(max_polls):
            self._check_deadline(deadline, task_id)
            remaining = deadline - time.monotonic()
            resp = requests.get(
                url,
                headers=self._headers(),
                timeout=min(30, max(1, remaining)),
                proxies=Config.requests_proxies("ark"),
            )
            resp.raise_for_status()
            data = resp.json()
            
            status = data.get("status")
            if status == "succeeded":
                # 根据实际返回体，URL 位于 content.video_url 或 video_url
                video_url = data.get("content", {}).get("video_url") or data.get("video_url")
                if not video_url:
                    raise RuntimeError(f"Seedance 任务成功但未返回视频 URL: {data}")
                return video_url
            elif status in ("failed", "expired"):
                error_msg = data.get("error", {}).get("message") or data.get("status_msg") or "未知错误"
                raise RuntimeError(f"Seedance 视频生成{status}: {error_msg}")
            
            logger.debug(f"SeedanceVideoClient: 任务进行中 {task_id}, status={status}, poll={i+1}")
            self._check_deadline(deadline, task_id)
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

        raise TimeoutError(f"Seedance 视频生成超时 (task_id={task_id})")

    def _download_video(self, url: str, save_path: str, timeout: int = 120):
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        resp = requests.get(url, stream=True, timeout=timeout, proxies=Config.requests_proxies("ark"))
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        logger.info(f"SeedanceVideoClient: 视频已保存: {save_path}")

    def cancel_task(self, task_id: str, *, transport=None) -> dict:
        """Cancel a queued content-generation task, or report why it was not cancelled.

        Official API (Volcengine Ark):

        ``DELETE https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}``

        https://www.volcengine.com/docs/82379/1521720
        https://ark.volcengine.com/region:cn-beijing/docs/cancel-or-delete-video-generation-tasks-api

        That page says success is HTTP 200 and ``{}``. What DELETE does
        depends on status. Status comes from the documented query
        ``GET .../contents/generations/tasks/{id}``
        (https://ark.volcengine.com/region:cn-beijing/docs/get-video-generation-task-api):

        - ``queued``: DELETE cancels the task and the status becomes ``cancelled``.
        - ``running``: DELETE is not supported. This method does not call it.
        - ``succeeded``, ``failed``, ``cancelled``, ``expired``: DELETE deletes
          the record and later queries are not supported. This method does
          not call DELETE for those. ``cancelled`` is already a cancel.
          ``succeeded`` is completion. ``failed`` and ``expired`` are not a cancel.

        The cancel page does not document the HTTP status of a rejected
        DELETE. Anything other than 200, a timeout, or a follow-up query that
        is not ``cancelled`` is not success. HTTP 200 alone is not enough,
        because the same code also means a finished record was deleted. A
        second query must show ``cancelled``. A follow-up ``succeeded`` is
        completion. A missing record after DELETE is uncertain.
        """
        from models.middle.adapter import (
            CANCEL_CANCELLED,
            CANCEL_COMPLETED,
            CANCEL_REJECTED,
            CANCEL_UNCERTAIN,
            CANCEL_UNSUPPORTED,
            cancel_result,
        )

        if not isinstance(task_id, str) or not task_id.strip():
            return cancel_result(
                CANCEL_UNCERTAIN,
                "没有厂商任务号，无法确认能否取消，报价保持保留，节点状态不变。",
            )
        task_id = task_id.strip()
        if transport is None and not self.api_key:
            return cancel_result(
                CANCEL_UNCERTAIN,
                "未配置视频厂商密钥，无法向厂商确认取消结果，报价保持保留，节点状态不变。",
            )
        try:
            current = self._read_task(task_id, transport=transport)
        except _CancelStopped as stopped:
            return stopped.result
        status = current.get("status")
        if not isinstance(status, str) or status not in _TASK_STATUSES:
            shown = status if isinstance(status, str) and status else "空"
            return cancel_result(
                CANCEL_UNCERTAIN,
                (
                    f"查询到的任务状态是 {shown}，不在官方文档列出的 queued、running、"
                    "cancelled、succeeded、failed、expired 之中，没有调用删除。"
                    "报价保持保留，节点状态不变。"
                ),
                task_status=status if isinstance(status, str) else None,
            )
        video_url = _task_video_url(current)
        if status == "succeeded":
            return cancel_result(
                CANCEL_COMPLETED,
                "查询到任务已成功。官方删除接口会删掉已完成任务的记录且之后不能查询，因此没有调用删除。报价不能释放。",
                video_url=video_url,
                task_status=status,
            )
        if status == "running":
            return cancel_result(
                CANCEL_UNSUPPORTED,
                "任务正在运行。官方文档写明 running 状态不支持取消或删除，没有调用删除。报价保持保留，节点状态不变。",
                task_status=status,
            )
        if status in {"failed", "expired"}:
            return cancel_result(
                CANCEL_REJECTED,
                (
                    f"任务状态是 {status}。官方取消接口只取消排队中的任务；对已结束任务调用删除会删掉记录"
                    "且之后不能查询，因此没有调用。报价保持保留，节点状态不变。"
                ),
                task_status=status,
            )
        if status == "cancelled":
            return cancel_result(
                CANCEL_CANCELLED,
                "查询到任务已是 cancelled。官方文档写明只有排队中的任务能被取消，且取消后状态为 cancelled。没有再调用删除（删除会清掉记录）。",
                task_status=status,
            )
        try:
            deleted = self._delete_task(task_id, transport=transport)
        except _CancelStopped as stopped:
            return stopped.result
        try:
            follow = self._read_task(task_id, transport=transport)
        except _CancelStopped:
            return cancel_result(
                CANCEL_UNCERTAIN,
                "取消接口返回 HTTP 200，但随后不能确认任务已变为 cancelled。已完成任务的删除会让记录无法再查询，因此这次结果不确定。报价保持保留，节点状态不变。",
                http_status=deleted,
            )
        follow_status = follow.get("status") if isinstance(follow.get("status"), str) else None
        follow_url = _task_video_url(follow)
        if follow_status == "cancelled":
            return cancel_result(
                CANCEL_CANCELLED,
                "排队中的任务已取消，随后查询到状态为 cancelled。",
                task_status=follow_status,
                http_status=deleted,
            )
        if follow_status == "succeeded":
            return cancel_result(
                CANCEL_COMPLETED,
                "取消请求返回 HTTP 200，但随后查询到任务已成功。按完成处理，没有把这次结果当成取消。",
                video_url=follow_url,
                task_status=follow_status,
                http_status=deleted,
            )
        shown = follow_status or "空"
        return cancel_result(
            CANCEL_UNCERTAIN,
            (
                f"取消接口返回 HTTP 200，但随后查询到的状态是 {shown}，不是官方写明的 cancelled。"
                "不能当成取消成功。报价保持保留，节点状态不变。"
            ),
            video_url=follow_url,
            task_status=follow_status,
            http_status=deleted,
        )

    def _task_url(self, task_id: str) -> str:
        from urllib.parse import quote

        return f"{self.base_url}/contents/generations/tasks/{quote(task_id, safe='')}"

    def _read_task(self, task_id: str, *, transport) -> dict:
        url = self._task_url(task_id)
        try:
            resp = self._http("GET", url, transport=transport)
        except _CancelStopped:
            raise
        except (requests.Timeout, requests.ConnectionError, TimeoutError) as exc:
            raise _CancelStopped(_uncertain_timeout("查询")) from exc
        except requests.RequestException as exc:
            raise _CancelStopped(_uncertain_transport("查询", exc)) from exc
        return self._require_task_body(resp, "查询")

    def _delete_task(self, task_id: str, *, transport) -> int:
        """DELETE one queued task. Returns the observed HTTP status when it is 200."""
        url = self._task_url(task_id)
        try:
            resp = self._http("DELETE", url, transport=transport)
        except _CancelStopped:
            raise
        except (requests.Timeout, requests.ConnectionError, TimeoutError) as exc:
            raise _CancelStopped(_uncertain_timeout("取消")) from exc
        except requests.RequestException as exc:
            raise _CancelStopped(_uncertain_transport("取消", exc)) from exc
        status = getattr(resp, "status_code", None)
        if status == 200:
            return 200
        from models.middle.adapter import CANCEL_REJECTED, CANCEL_UNCERTAIN, cancel_result

        if isinstance(status, int):
            raise _CancelStopped(
                cancel_result(
                    CANCEL_REJECTED,
                    (
                        f"厂商没有返回取消成功（HTTP {status}）。官方文档只说明成功时 HTTP 200，"
                        "且排队中的任务会变为 cancelled。该状态码的含义文档未说明，不能当成取消成功。"
                        "报价保持保留，节点状态不变。"
                    ),
                    http_status=status,
                )
            )
        raise _CancelStopped(
            cancel_result(
                CANCEL_UNCERTAIN,
                "取消请求没有返回可核对的 HTTP 状态码，结果不确定。报价保持保留，节点状态不变。",
            )
        )

    def _require_task_body(self, resp, action: str) -> dict:
        from models.middle.adapter import CANCEL_UNCERTAIN, cancel_result

        status = getattr(resp, "status_code", None)
        if status != 200:
            shown = status if isinstance(status, int) else "未知"
            if action == "查询":
                detail = (
                    f"{action}任务返回 HTTP {shown}。官方查询文档没有把该状态码定义成可取消或已完成，"
                    "没有调用删除。报价保持保留，节点状态不变。"
                )
            else:
                detail = (
                    f"{action}请求返回 HTTP {shown}，官方文档未说明该状态码，结果不确定。"
                    "报价保持保留，节点状态不变。"
                )
            raise _CancelStopped(
                cancel_result(
                    CANCEL_UNCERTAIN,
                    detail,
                    http_status=status if isinstance(status, int) else None,
                )
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise _CancelStopped(
                cancel_result(
                    CANCEL_UNCERTAIN,
                    f"{action}返回 HTTP 200，但响应体不是文档中的 JSON 对象，结果不确定。报价保持保留，节点状态不变。",
                    http_status=200,
                )
            ) from exc
        if not isinstance(data, dict):
            raise _CancelStopped(
                cancel_result(
                    CANCEL_UNCERTAIN,
                    f"{action}返回 HTTP 200，但响应体不是 JSON 对象，结果不确定。报价保持保留，节点状态不变。",
                    http_status=200,
                )
            )
        return data

    def _http(self, method: str, url: str, *, transport):
        kwargs = {
            "headers": self._headers(),
            "timeout": CANCEL_HTTP_TIMEOUT_SECONDS,
            "proxies": Config.requests_proxies("ark"),
        }
        if transport is not None:
            fn = getattr(transport, method.lower(), None)
            if not callable(fn):
                from models.middle.adapter import CANCEL_UNCERTAIN, cancel_result

                raise _CancelStopped(
                    cancel_result(
                        CANCEL_UNCERTAIN,
                        "取消通道没有可用的查询或删除方法，结果不确定。报价保持保留，节点状态不变。",
                    )
                )
            return fn(url, **kwargs)
        if method == "GET":
            return requests.get(url, **kwargs)
        if method == "DELETE":
            return requests.delete(url, **kwargs)
        raise RuntimeError(f"unsupported cancel method {method}")


class _CancelStopped(Exception):
    """A cancel attempt already has a non-success result."""

    def __init__(self, result: dict) -> None:
        self.result = result
        super().__init__(result.get("message") or "cancel stopped")


def _task_video_url(data: dict) -> Optional[str]:
    """URL from a query body. The documented field is ``content.video_url``."""
    content = data.get("content")
    if isinstance(content, dict):
        url = content.get("video_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    url = data.get("video_url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _uncertain_timeout(action: str) -> dict:
    from models.middle.adapter import CANCEL_UNCERTAIN, cancel_result

    return cancel_result(
        CANCEL_UNCERTAIN,
        f"{action}超时。官方文档没有规定该超时，无法确认厂商是否已取消。报价保持保留，节点状态不变。",
    )


def _uncertain_transport(action: str, exc: BaseException) -> dict:
    from models.middle.adapter import CANCEL_UNCERTAIN, cancel_result

    del exc
    return cancel_result(
        CANCEL_UNCERTAIN,
        f"{action}请求失败，没有可核对的官方成功结果。报价保持保留，节点状态不变。",
    )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config import Config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # ── 测试参数（按需修改） ──
    # IMAGE_PATH = "code/result/image/test_avail/test_input.png"
    IMAGE_PATH = "code/result/image/test_avail/test_input_human.jpg"
    OUTPUT_PATH = "code/result/video/test_avail/seedance_test_output.mp4"
    PROMPT = "女生把财务报表交给男生，男生看到后喜极而泣"
    # MODELS = ["doubao-seedance-2-0-fast-260128", "doubao-seedance-2-0-260128"]
    MODELS = ["doubao-seedance-2-0-fast-260128"]
    DURATION = 5

    print("=== Seedance (ARK) 图生视频测试 ===")
    api_key = Config.ARK_API_KEY
    base_url = Config.ARK_BASE_URL
    
    if not api_key:
        print("✗ ARK_API_KEY 未设置，请检查 config.yaml 配置")
        sys.exit(1)

    if not os.path.exists(IMAGE_PATH):
        print(f"✗ 输入图片不存在: {IMAGE_PATH}")
        sys.exit(1)

    print(f"  API Key    : {api_key[:6]}***{api_key[-4:]}")
    print(f"  Base URL   : {base_url}")

    for model in MODELS:
        print("\n" + "="*40)
        print(f"  输入图片   : {IMAGE_PATH}")
        print(f"  输出路径   : {OUTPUT_PATH}")
        print(f"  模型       : {model}")
        print(f"  时长       : {DURATION}s")
        if PROMPT:
            print(f"  提示词     : {PROMPT[:80]}")

        try:
            client = SeedanceVideoClient(api_key=api_key, base_url=base_url)
            print("✓ 客户端初始化成功")

            start = time.time()
            video_url = client.generate_video(
                prompt=PROMPT,
                image_path=IMAGE_PATH,
                save_path=OUTPUT_PATH,
                model=model,
                duration=DURATION,
            )
            elapsed = time.time() - start

            print(f"✓ 视频生成完成！耗时 {elapsed:.1f}s")
            print(f"  远端 URL : {video_url}")
            print(f"  本地文件 : {os.path.abspath(OUTPUT_PATH)}")
            print(f"  文件大小 : {os.path.getsize(OUTPUT_PATH) / 1024 / 1024:.2f} MB")
        except Exception as e:
            print(f"✗ 失败: {e}")
            sys.exit(1)
        break  # 只测试第一个模型
